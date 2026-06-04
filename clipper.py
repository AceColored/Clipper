using System.Diagnostics;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Drawing.Text;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Win32;

namespace Clipper;

static class Program
{
    static readonly string LogFile = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "clipper_crash.log");

    static void Log(string msg) {
        try { File.AppendAllText(LogFile, $"[{DateTime.Now:HH:mm:ss}] {msg}\n"); } catch { }
    }

    [STAThread]
    static void Main()
    {
        File.WriteAllText(LogFile, $"--- session {DateTime.Now} ---\n");
        try
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
            Application.ThreadException += (_, e) => {
                Log("thread ex: " + e.Exception);
                MessageBox.Show(e.Exception.Message, "Clipper error");
            };
            AppDomain.CurrentDomain.UnhandledException += (_, e) => Log("unhandled: " + e.ExceptionObject);
            Log("ok");
            Application.Run(new MainForm());
        }
        catch (Exception ex)
        {
            Log("fatal: " + ex);
            MessageBox.Show(ex.ToString(), "Fatal error");
        }
    }
}

public class ClipperConfig
{
    // defaults that made sense after testing on a few machines
    [JsonPropertyName("monitor")]    public int     MonitorIdx  { get; set; } = 0;
    [JsonPropertyName("resolution")] public string  Res         { get; set; } = "1080p";
    [JsonPropertyName("fps")]        public int     Fps         { get; set; } = 30;
    [JsonPropertyName("audio_name")] public string? AudioDevice { get; set; }
    [JsonPropertyName("duration")]   public string  ClipLen     { get; set; } = "30 sec";
    [JsonPropertyName("hotkey")]     public string  Hotkey      { get; set; } = "ctrl+shift+s";
    [JsonPropertyName("output_dir")] public string  OutDir      { get; set; } =
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "clips");
    [JsonPropertyName("topmost")]    public bool    Topmost     { get; set; } = false;

    static string SettingsPath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".clipper.json");

    public static ClipperConfig Load()
    {
        if (!File.Exists(SettingsPath)) return new();
        try {
            return JsonSerializer.Deserialize<ClipperConfig>(File.ReadAllText(SettingsPath)) ?? new();
        }
        catch (JsonException) {
            // corrupted — nuke it and start clean rather than crashing on every launch
            try { File.Delete(SettingsPath); } catch { }
            return new();
        }
    }

    public void Persist()
    {
        try {
            File.WriteAllText(SettingsPath,
                JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true }));
        } catch (UnauthorizedAccessException) {
            // sandboxed or read-only profile, nothing we can do
        }
    }
}

// generic sliding window — oldest entries drop off automatically
public class RingBuffer<T>
{
    readonly LinkedList<(double when, T payload)> items = new();
    readonly object gate = new();
    double keepSecs = 65;

    public double WindowSecs { set { lock (gate) keepSecs = value; } }

    public void Add(double ts, T item)
    {
        lock (gate)
        {
            items.AddLast((ts, item));
            double oldest = ts - keepSecs;
            while (items.First?.Value.when < oldest)
                items.RemoveFirst();
        }
    }

    public List<(double when, T payload)> Since(double cutoff)
    {
        lock (gate) return items.Where(x => x.when >= cutoff).ToList();
    }

    public int ApproxCount { get { lock (gate) return items.Count; } }
    public void Clear()    { lock (gate) items.Clear(); }
}

static class FfmpegFinder
{
    public static string? Locate()
    {
        // shipped alongside the exe takes priority so users can pin a version
        var next2exe = Path.Combine(AppContext.BaseDirectory, "ffmpeg.exe");
        if (File.Exists(next2exe)) return next2exe;

        foreach (var dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(';'))
        {
            var candidate = Path.Combine(dir.Trim(), "ffmpeg.exe");
            if (File.Exists(candidate)) return candidate;
        }

        // people install ffmpeg and forget to add it to PATH all the time
        string[] knownSpots = [
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "ffmpeg", "bin", "ffmpeg.exe"),
            @"C:\ffmpeg\bin\ffmpeg.exe",
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "ffmpeg", "bin", "ffmpeg.exe"),
        ];
        return knownSpots.FirstOrDefault(File.Exists);
    }
}

public record DisplayInfo(Rectangle Rect, string Label)
{
    public override string ToString() => $"{Label}  ({Rect.Width}×{Rect.Height})";
}

static class DisplayHelper
{
    public static List<DisplayInfo> Enumerate()
    {
        var screens = Screen.AllScreens;
        var names   = TryReadEdidNames();
        return screens.Select((s, i) => {
            var name = i < names.Count && !string.IsNullOrEmpty(names[i]) ? names[i] : $"Display {i + 1}";
            return new DisplayInfo(s.Bounds, name);
        }).ToList();
    }

    static List<string> TryReadEdidNames()
    {
        var result = new List<string>();
        try
        {
            var dd = new DISPLAY_DEVICE { cb = Marshal.SizeOf<DISPLAY_DEVICE>() };
            for (uint i = 0; EnumDisplayDevices(null, i, ref dd, 0); i++)
            {
                if ((dd.StateFlags & 1) != 0)
                {
                    var mon = new DISPLAY_DEVICE { cb = Marshal.SizeOf<DISPLAY_DEVICE>() };
                    EnumDisplayDevices(dd.DeviceName, 0, ref mon, 1);
                    result.Add(ParseEdidMonitorName(mon.DeviceID) ?? "");
                }
                dd = new DISPLAY_DEVICE { cb = Marshal.SizeOf<DISPLAY_DEVICE>() };
            }
        }
        catch { /* EDID read failed — we'll fall back to "Display N" labels */ }
        return result;
    }

    static string? ParseEdidMonitorName(string deviceId)
    {
        try
        {
            var match = System.Text.RegularExpressions.Regex.Match(deviceId, @"DISPLAY#([^#]+)#");
            if (!match.Success) return null;

            using var key = Registry.LocalMachine.OpenSubKey(
                $@"SYSTEM\CurrentControlSet\Enum\DISPLAY\{match.Groups[1].Value}");
            if (key == null) return null;

            foreach (var sub in key.GetSubKeyNames())
            {
                using var pk = key.OpenSubKey($@"{sub}\Device Parameters");
                if (pk?.GetValue("EDID") is not byte[] edid) continue;

                // EDID spec: descriptor blocks start at offset 54, each 18 bytes
                // type byte 0xFC = monitor name string
                for (int j = 0; j < 4; j++)
                {
                    int o = 54 + j * 18;
                    if (edid.Length < o + 18) break;
                    if (edid[o+3] == 0xFC)
                        return System.Text.Encoding.ASCII.GetString(edid, o + 5, 13).TrimEnd('\n', ' ');
                }
            }
        }
        catch (Exception) { }
        return null;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct DISPLAY_DEVICE
    {
        public int cb;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]  public string DeviceName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceString;
        public uint StateFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceID;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceKey;
    }

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern bool EnumDisplayDevices(string? lpDevice, uint iDevNum, ref DISPLAY_DEVICE lpDD, uint dwFlags);
}

// thin progress bar drawn manually so we can match the dark theme exactly
public class ThinBar : Control
{
    double cur, cap = 1;
    static readonly Color empty  = Color.FromArgb(0x28, 0x28, 0x28);
    static readonly Color filled = Color.FromArgb(0xCC, 0xCC, 0xCC);

    public ThinBar() { DoubleBuffered = true; Height = 2; }

    public void SetProgress(double value, double maximum)
    {
        cur = value; cap = maximum > 0 ? maximum : 1;
        Invalidate();
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        e.Graphics.FillRectangle(new SolidBrush(empty), 0, 0, Width, Height);
        if (cur > 0) {
            int px = (int)(Width * cur / cap);
            if (px > 0) e.Graphics.FillRectangle(new SolidBrush(filled), 0, 0, px, Height);
        }
    }
}

public class RoundButton : Control
{
    bool hovering, pressing;
    readonly Action onClick;

    static readonly Color normalBg   = Color.FromArgb(0x28, 0x28, 0x28);
    static readonly Color hoverBg    = Color.FromArgb(0x38, 0x38, 0x38);
    static readonly Color pressBg    = Color.FromArgb(0x1A, 0x1A, 0x1A);
    static readonly Color disabledBg = Color.FromArgb(0x1C, 0x1C, 0x1C);
    static readonly Color normalEdge = Color.FromArgb(0x50, 0x50, 0x50);
    static readonly Color hoverEdge  = Color.FromArgb(0x78, 0x78, 0x78);
    static readonly Color normalText = Color.FromArgb(0xEE, 0xEE, 0xEE);
    static readonly Color dimText    = Color.FromArgb(0x48, 0x48, 0x48);
    // subtle top gradient to break the flat look
    static readonly Color topGlow   = Color.FromArgb(0x48, 0xFF, 0xFF, 0xFF);
    const int rad = 7;

    public RoundButton(string label, Action action, int w, int h = 30)
    {
        Text = label; onClick = action;
        Size = new Size(w, h);
        Font = new Font("Segoe UI", 9f);
        Cursor = Cursors.Hand;
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.UserPaint | ControlStyles.OptimizedDoubleBuffer, true);
    }

    protected override void OnEnabledChanged(EventArgs e) { Invalidate(); base.OnEnabledChanged(e); }
    protected override void OnMouseEnter(EventArgs e) { hovering = true;  Invalidate(); base.OnMouseEnter(e); }
    protected override void OnMouseLeave(EventArgs e) { hovering = pressing = false; Invalidate(); base.OnMouseLeave(e); }
    protected override void OnMouseDown(MouseEventArgs e)
    {
        if (e.Button == MouseButtons.Left) { pressing = true; Invalidate(); }
        base.OnMouseDown(e);
    }
    protected override void OnMouseUp(MouseEventArgs e) { pressing = false; Invalidate(); base.OnMouseUp(e); }
    protected override void OnClick(EventArgs e) { if (Enabled) onClick(); base.OnClick(e); }

    protected override void OnPaint(PaintEventArgs e)
    {
        var g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit;
        g.PixelOffsetMode = PixelOffsetMode.HighQuality;

        var r    = new Rectangle(1, 1, Width - 2, Height - 2);
        var bg   = !Enabled ? disabledBg : pressing ? pressBg : hovering ? hoverBg : normalBg;
        var edge = hovering && Enabled ? hoverEdge : normalEdge;
        var fg   = Enabled ? normalText : dimText;

        using var outline = MakeRoundRect(r, rad);
        g.FillPath(new SolidBrush(bg), outline);

        if (Enabled && !pressing) {
            var topHalf = new RectangleF(r.X, r.Y, r.Width, r.Height / 2f);
            using var topPath = MakeRoundRect(Rectangle.Round(topHalf), rad);
            using var grd = new LinearGradientBrush(
                new PointF(0, r.Y), new PointF(0, r.Y + r.Height / 2f), topGlow, Color.Transparent);
            g.FillPath(grd, topPath);
        }

        g.DrawPath(new Pen(edge, 1f) { Alignment = PenAlignment.Inset }, outline);

        using var sf = new StringFormat {
            Alignment = StringAlignment.Center,
            LineAlignment = StringAlignment.Center,
            Trimming = StringTrimming.EllipsisCharacter
        };
        g.DrawString(Text, Font, new SolidBrush(fg),
            new RectangleF(0, pressing ? 1f : 0f, Width, Height), sf);
    }

    static GraphicsPath MakeRoundRect(Rectangle r, int corner)
    {
        int d = corner * 2;
        var p = new GraphicsPath();
        p.AddArc(r.X,         r.Y,          d, d, 180, 90);
        p.AddArc(r.Right - d, r.Y,          d, d, 270, 90);
        p.AddArc(r.Right - d, r.Bottom - d, d, d,   0, 90);
        p.AddArc(r.X,         r.Bottom - d, d, d,  90, 90);
        p.CloseFigure();
        return p;
    }
}

// wraps a borderless TextBox in a panel we can draw our own border on
public class StyledTextBox : Control
{
    readonly TextBox inner;

    static readonly Color bgColor    = Color.FromArgb(0x1E, 0x1E, 0x1E);
    static readonly Color edgeNormal = Color.FromArgb(0x3C, 0x3C, 0x3C);
    static readonly Color edgeFocused= Color.FromArgb(0x70, 0x70, 0x70);
    static readonly Color textColor  = Color.FromArgb(0xE2, 0xE2, 0xE2);

    public new string Text { get => inner.Text; set => inner.Text = value; }
    public new Font   Font { get => inner.Font; set => inner.Font = value; }
    public new event EventHandler?    TextChanged { add => inner.TextChanged += value; remove => inner.TextChanged -= value; }
    public new event KeyEventHandler? KeyDown     { add => inner.KeyDown    += value; remove => inner.KeyDown    -= value; }
    public new event EventHandler?    Leave       { add => inner.Leave      += value; remove => inner.Leave      -= value; }

    public StyledTextBox(string initial = "")
    {
        BackColor = bgColor;
        inner = new TextBox {
            BorderStyle = BorderStyle.None,
            BackColor   = bgColor,
            ForeColor   = textColor,
            Font        = new Font("Segoe UI", 9f),
            Text        = initial,
        };
        inner.GotFocus  += (_, _) => Invalidate();
        inner.LostFocus += (_, _) => Invalidate();
        Controls.Add(inner);
        SetStyle(ControlStyles.UserPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint, true);
    }

    public new void Focus() => inner.Focus();

    protected override void OnSizeChanged(EventArgs e)
    {
        base.OnSizeChanged(e);
        inner.SetBounds(8, (Height - inner.PreferredHeight) / 2, Width - 16, inner.PreferredHeight);
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        e.Graphics.Clear(bgColor);
        e.Graphics.DrawRectangle(
            new Pen(inner.Focused ? edgeFocused : edgeNormal, 1f),
            0, 0, Width - 1, Height - 1);
    }
}

public class DropPicker : Control
{
    static readonly Color bgOff  = Color.FromArgb(0x1E, 0x1E, 0x1E);
    static readonly Color bgHov  = Color.FromArgb(0x27, 0x27, 0x27);
    static readonly Color bgOpen = Color.FromArgb(0x22, 0x22, 0x22);
    static readonly Color rimOff = Color.FromArgb(0x3C, 0x3C, 0x3C);
    static readonly Color rimOn  = Color.FromArgb(0x68, 0x68, 0x68);
    static readonly Color itemFg = Color.FromArgb(0xE2, 0xE2, 0xE2);
    static readonly Color arrOff = Color.FromArgb(0x72, 0x72, 0x72);
    static readonly Color arrOn  = Color.FromArgb(0xB0, 0xB0, 0xB0);

    readonly List<string> choices = [];
    int  selectedIdx = -1;
    bool mouseOver, blockClick;
    FlyoutList? flyout;

    public int SelectedIndex
    {
        get => selectedIdx;
        set {
            if (selectedIdx == value) return;
            selectedIdx = Math.Max(-1, value);
            Invalidate();
            SelectedIndexChanged?.Invoke(this, EventArgs.Empty);
        }
    }

    public object? SelectedItem
    {
        get => selectedIdx >= 0 && selectedIdx < choices.Count ? choices[selectedIdx] : null;
        set {
            var s = value?.ToString();
            SelectedIndex = s == null ? -1 : choices.IndexOf(s);
        }
    }

    public event EventHandler? SelectedIndexChanged;

    public DropPicker()
    {
        Font = new Font("Segoe UI", 9f);
        Cursor = Cursors.Hand;
        Height = 26;
        SetStyle(ControlStyles.UserPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint, true);
    }

    public void SetItems(string[] items) { choices.AddRange(items); Invalidate(); }
    public void ClearItems() { choices.Clear(); selectedIdx = -1; Invalidate(); }

    protected override void OnMouseEnter(EventArgs e) { mouseOver = true;  Invalidate(); base.OnMouseEnter(e); }
    protected override void OnMouseLeave(EventArgs e) { mouseOver = false; Invalidate(); base.OnMouseLeave(e); }

    protected override void OnClick(EventArgs e)
    {
        base.OnClick(e);
        if (blockClick) return;
        if (flyout != null) { CloseFlyout(); return; }
        ShowFlyout();
    }

    void ShowFlyout()
    {
        if (choices.Count == 0) return;
        var pos = PointToScreen(new Point(0, Height - 1));
        flyout = new FlyoutList(choices, selectedIdx, Width, pos);
        flyout.ItemChosen += (_, i) => { SelectedIndex = i; CloseFlyout(); };
        flyout.Closed     += (_, _) => { flyout = null; Invalidate(); };
        flyout.Show(FindForm());
        Invalidate();
    }

    void CloseFlyout()
    {
        flyout?.Close();
        flyout = null;
        blockClick = true;
        var t = new System.Windows.Forms.Timer { Interval = 150 };
        t.Tick += (_, _) => { blockClick = false; t.Stop(); t.Dispose(); };
        t.Start();
        Invalidate();
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        var g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit;
        g.PixelOffsetMode = PixelOffsetMode.HighQuality;

        bool open = flyout != null;
        var bg  = open ? bgOpen : mouseOver ? bgHov : bgOff;
        var rim = open || mouseOver ? rimOn : rimOff;

        g.FillRectangle(new SolidBrush(bg),  new Rectangle(0, 0, Width - 1, Height - 1));
        g.DrawRectangle(new Pen(rim, 1f),     new Rectangle(0, 0, Width - 1, Height - 1));

        using var sf = new StringFormat {
            LineAlignment = StringAlignment.Center,
            Trimming      = StringTrimming.EllipsisCharacter,
            FormatFlags   = StringFormatFlags.NoWrap,
        };
        g.DrawString(SelectedItem?.ToString() ?? "", Font, new SolidBrush(itemFg),
            new RectangleF(9, 0, Width - 26, Height), sf);

        // chevron flips when open
        int cx = Width - 13, cy = Height / 2;
        PointF[] tri = open
            ? [new(cx - 3.5f, cy + 1.5f), new(cx + 3.5f, cy + 1.5f), new(cx, cy - 2.5f)]
            : [new(cx - 3.5f, cy - 1.5f), new(cx + 3.5f, cy - 1.5f), new(cx, cy + 2.5f)];
        g.FillPolygon(new SolidBrush(open ? arrOn : arrOff), tri);
    }
}

// the actual dropdown list that pops up
internal class FlyoutList : Form
{
    static readonly Color bg      = Color.FromArgb(0x1E, 0x1E, 0x1E);
    static readonly Color hoverBg = Color.FromArgb(0x2A, 0x2A, 0x2A);
    static readonly Color selBg   = Color.FromArgb(0x2F, 0x2F, 0x2F);
    static readonly Color border  = Color.FromArgb(0x44, 0x44, 0x44);
    static readonly Color textFg  = Color.FromArgb(0xD8, 0xD8, 0xD8);
    static readonly Color selFg   = Color.FromArgb(0xFF, 0xFF, 0xFF);
    static readonly Color sbBg    = Color.FromArgb(0x28, 0x28, 0x28);
    static readonly Color sbFg    = Color.FromArgb(0x48, 0x48, 0x48);
    static readonly Color sbHov   = Color.FromArgb(0x60, 0x60, 0x60);
    static readonly Font  rowFont = new("Segoe UI", 9f);

    const int ROW_H = 26, MAX_ROWS = 7, SB_W = 6;

    readonly List<string> options;
    readonly int currentSel;
    int hovered = -1, scrollTop = 0;
    bool sbHovered;

    public event EventHandler<int>? ItemChosen;

    public FlyoutList(List<string> items, int current, int width, Point screenPos)
    {
        options    = items;
        currentSel = current;

        int rows  = Math.Min(items.Count, MAX_ROWS);
        int totalH = rows * ROW_H + 2;

        FormBorderStyle = FormBorderStyle.None;
        StartPosition   = FormStartPosition.Manual;
        BackColor       = bg;
        ShowInTaskbar   = false;
        Size = new Size(width, totalH);

        // if the popup would go off the bottom of the screen, show it above instead
        var workArea = Screen.FromPoint(screenPos).WorkingArea;
        int y = screenPos.Y + totalH > workArea.Bottom
            ? screenPos.Y - totalH - 26
            : screenPos.Y;
        Location = new Point(screenPos.X, y);

        // start scroll position so selected item is visible
        if (current > MAX_ROWS / 2)
            scrollTop = Math.Max(0, Math.Min(current - MAX_ROWS / 2, items.Count - MAX_ROWS));

        SetStyle(ControlStyles.UserPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint, true);
        MouseMove  += HandleMouseMove;
        MouseLeave += (_, _) => { hovered = -1; sbHovered = false; Invalidate(); };
        MouseDown  += HandleClick;
        MouseWheel += HandleScroll;
        Deactivate += (_, _) => Close();
        KeyDown    += HandleKey;
    }

    protected override bool ShowWithoutActivation => false;

    int RowCount  => Math.Min(options.Count, MAX_ROWS);
    bool NeedsSB  => options.Count > MAX_ROWS;

    void HandleMouseMove(object? s, MouseEventArgs e)
    {
        bool inSB = NeedsSB && e.X >= Width - SB_W - 2;
        bool dirty = sbHovered != inSB;
        sbHovered = inSB;
        int idx = inSB ? -1 : (e.Y - 1) / ROW_H + scrollTop;
        if (idx != hovered && idx < options.Count) { hovered = idx; dirty = true; }
        if (dirty) Invalidate();
    }

    void HandleClick(object? s, MouseEventArgs e)
    {
        if (e.Button != MouseButtons.Left) return;
        if (NeedsSB && e.X >= Width - SB_W - 2) return; // scrollbar click, ignore
        int idx = (e.Y - 1) / ROW_H + scrollTop;
        if (idx >= 0 && idx < options.Count) ItemChosen?.Invoke(this, idx);
    }

    void HandleScroll(object? s, MouseEventArgs e)
    {
        if (!NeedsSB) return;
        scrollTop = Math.Max(0, Math.Min(scrollTop - e.Delta / 120, options.Count - MAX_ROWS));
        Invalidate();
    }

    void HandleKey(object? s, KeyEventArgs e)
    {
        switch (e.KeyCode)
        {
            case Keys.Escape: Close(); break;
            case Keys.Up:     Nudge(-1); break;
            case Keys.Down:   Nudge(1);  break;
            case Keys.Return:
                if (hovered >= 0) ItemChosen?.Invoke(this, hovered);
                break;
        }
    }

    void Nudge(int dir)
    {
        int next = Math.Max(0, Math.Min((hovered < 0 ? currentSel : hovered) + dir, options.Count - 1));
        hovered = next;
        if (hovered < scrollTop) scrollTop = hovered;
        if (hovered >= scrollTop + MAX_ROWS) scrollTop = hovered - MAX_ROWS + 1;
        Invalidate();
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        var g = e.Graphics;
        g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.FillRectangle(new SolidBrush(bg), 0, 0, Width, Height);

        int textW = NeedsSB ? Width - SB_W - 10 : Width - 16;

        for (int vi = 0; vi < RowCount; vi++)
        {
            int di = vi + scrollTop;
            if (di >= options.Count) break;

            var rowRect = new Rectangle(1, vi * ROW_H + 1, Width - 2, ROW_H);
            bool isHov = di == hovered;
            bool isSel = di == currentSel;

            if (isHov)      g.FillRectangle(new SolidBrush(hoverBg), rowRect);
            else if (isSel) g.FillRectangle(new SolidBrush(selBg),   rowRect);

            // 2px left accent bar for the currently selected item
            if (isSel)
                g.FillRectangle(Brushes.Gray, new Rectangle(1, rowRect.Y, 2, rowRect.Height));

            using var sf = new StringFormat {
                LineAlignment = StringAlignment.Center,
                Trimming      = StringTrimming.EllipsisCharacter,
                FormatFlags   = StringFormatFlags.NoWrap
            };
            g.DrawString(options[di], rowFont,
                new SolidBrush(isHov || isSel ? selFg : textFg),
                new RectangleF(10, rowRect.Y, textW, rowRect.Height), sf);
        }

        if (NeedsSB)
        {
            int tx = Width - SB_W - 1, th = Height - 2;
            g.FillRectangle(new SolidBrush(sbBg), tx, 1, SB_W, th);
            float pct = (float)MAX_ROWS / options.Count;
            float pos = (float)scrollTop / options.Count;
            int tH = Math.Max(20, (int)(th * pct));
            int tY = 1 + (int)((th - tH) * pos);
            g.FillRectangle(new SolidBrush(sbHovered ? sbHov : sbFg), tx + 1, tY, SB_W - 2, tH);
        }

        g.DrawRectangle(new Pen(border, 1f), 0, 0, Width - 1, Height - 1);
    }

    protected override void OnShown(EventArgs e) { base.OnShown(e); Focus(); }
}

public record WavFormat(ushort Tag, ushort Channels, uint SampleRate, ushort BitDepth)
{
    public uint   BytesPerSec  => SampleRate * Channels * BitDepth / 8u;
    public ushort BlockAlign   => (ushort)(Channels * BitDepth / 8);
}

public class LoopbackRecorder : IDisposable
{
    // WASAPI GUIDs and constants — not worth pulling in an extra dependency for these
    const int CLSCTX_ALL = 23, eRender = 0, eConsole = 1, STGM_READ = 0, SHARED_MODE = 0;
    const int S_OK = 0;
    const uint LOOPBACK_FLAG = 0x00020000, AUTOCONVERT = 0x80000000, SRC_QUALITY = 0x08000000;
    const uint SILENT_PACKET = 2;
    const int TARGET_SR = 44100; // 44.1k is plenty for clips, no need for 48k

    static readonly Guid MMDevEnum_CLSID  = new("BCDE0395-E52F-467C-8E3D-C4579291692E");
    static readonly Guid MMDevEnum_IID    = new("A95664D2-9614-4F35-A746-DE8DB63617E6");
    static readonly Guid AudioClient_IID  = new("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2");
    static readonly Guid CapClient_IID    = new("C8ADBD64-E71E-48A0-A4DE-185C395CD317");
    static readonly Guid FriendlyName_Key = new("A45C254E-DF1C-4EFD-8020-67D146A850E0");
    const uint FriendlyName_PID = 14;

    readonly string? targetDevice;
    readonly Action<byte[], int> dataCallback;
    Thread? loopThread;
    volatile bool running;
    readonly ManualResetEventSlim startedEvent = new(false);

    public WavFormat? CaptureFormat { get; private set; }
    public int LatePackets { get; private set; }

    public LoopbackRecorder(string? deviceName, Action<byte[], int> onData)
    {
        targetDevice = deviceName;
        dataCallback = onData;
    }

    public bool Start()
    {
        running    = true;
        loopThread = new Thread(ThreadMain) { IsBackground = true, Name = "wasapi-loopback" };
        loopThread.Start();
        bool started = startedEvent.Wait(3000); // 3s should be more than enough for any normal device
        if (!started) {
            running = false;
            return false;
        }
        return true;
    }

    public void Stop() { running = false; loopThread?.Join(3000); }

    void ThreadMain()
    {
        CoInitializeEx(IntPtr.Zero, 0); // STA — required before any COM calls on this thread
        try   { CaptureAudio(); }
        catch (Exception ex) { System.Diagnostics.Debug.WriteLine("loopback thread: " + ex.Message); }
        finally { CoUninitialize(); }
    }

    void CaptureAudio()
    {
        var clsid = MMDevEnum_CLSID; var iid = MMDevEnum_IID;
        if (CoCreateInstance(ref clsid, IntPtr.Zero, CLSCTX_ALL, ref iid, out var raw) != S_OK) return;
        var enumerator = (IMMDeviceEnumerator)raw;

        try
        {
            var device = targetDevice != null
                ? (FindDeviceByName(enumerator, targetDevice) ?? GetDefault(enumerator))
                : GetDefault(enumerator);

            var acIID = AudioClient_IID;
            if (device.Activate(ref acIID, CLSCTX_ALL, IntPtr.Zero, out var clientRaw) != S_OK) return;
            var audioClient = (IAudioClient)clientRaw;

            try
            {
                if (audioClient.GetMixFormat(out var nativeFmtPtr) != S_OK) return;
                var nativeFmt = Marshal.PtrToStructure<WAVEFORMATEX>(nativeFmtPtr);
                Marshal.FreeCoTaskMem(nativeFmtPtr);

                // request float32 @ 44100 — cleaner than whatever the device native format is
                // AUTOCONVERT flag lets the driver resample for us
                var wantedFmt = new WAVEFORMATEX {
                    wFormatTag      = 3, // IEEE_FLOAT
                    nChannels       = 2,
                    nSamplesPerSec  = TARGET_SR,
                    wBitsPerSample  = 32,
                    nBlockAlign     = 8,
                    nAvgBytesPerSec = TARGET_SR * 8,
                    cbSize          = 0,
                };

                int hr = audioClient.Initialize(SHARED_MODE, LOOPBACK_FLAG | AUTOCONVERT | SRC_QUALITY,
                    10_000_000, 0, ref wantedFmt, IntPtr.Zero);

                if (hr != S_OK) {
                    // some drivers refuse AUTOCONVERT — fall back to whatever the device outputs natively
                    hr = audioClient.Initialize(SHARED_MODE, LOOPBACK_FLAG, 10_000_000, 0, ref nativeFmt, IntPtr.Zero);
                    if (hr != S_OK) return;
                    CaptureFormat = new WavFormat(nativeFmt.wFormatTag, nativeFmt.nChannels, nativeFmt.nSamplesPerSec, nativeFmt.wBitsPerSample);
                } else {
                    CaptureFormat = new WavFormat(3, 2, TARGET_SR, 32);
                }

                var ccIID = CapClient_IID;
                if (audioClient.GetService(ref ccIID, out var capRaw) != S_OK) return;
                var captureClient = (IAudioCaptureClient)capRaw;

                try
                {
                    audioClient.Start();
                    startedEvent.Set();
                    int frameBytes = CaptureFormat.BlockAlign;

                    while (running)
                    {
                        captureClient.GetNextPacketSize(out uint frameCount);
                        if (frameCount == 0) { Thread.Sleep(5); continue; }

                        int ghr = captureClient.GetBuffer(out var ptr, out uint nFrames, out uint flags, out _, out _);
                        if (ghr == S_OK && nFrames > 0)
                        {
                            int byteCount = (int)(nFrames * (uint)frameBytes);
                            var buf = new byte[byteCount];
                            // AUDCLNT_BUFFERFLAGS_SILENT (0x2) means the device output silence — send zeroed buf
                            if ((flags & SILENT_PACKET) == 0 && ptr != IntPtr.Zero)
                                Marshal.Copy(ptr, buf, 0, byteCount);
                            dataCallback(buf, byteCount);
                            captureClient.ReleaseBuffer(nFrames);
                        }
                        else LatePackets++;
                    }

                    audioClient.Stop();
                }
                finally { Marshal.ReleaseComObject(captureClient); }
            }
            finally { Marshal.ReleaseComObject(audioClient); }
        }
        finally { Marshal.ReleaseComObject(enumerator); }
    }

    static IMMDevice GetDefault(IMMDeviceEnumerator e)
    {
        e.GetDefaultAudioEndpoint(eRender, eConsole, out var d);
        return d;
    }

    static IMMDevice? FindDeviceByName(IMMDeviceEnumerator e, string name)
    {
        e.EnumAudioEndpoints(eRender, 1, out var col);
        col.GetCount(out uint n);
        for (uint i = 0; i < n; i++)
        {
            col.Item(i, out var dev);
            if (GetFriendlyName(dev) == name) return dev;
            Marshal.ReleaseComObject(dev);
        }
        return null;
    }

    static string? GetFriendlyName(IMMDevice dev)
    {
        try {
            dev.OpenPropertyStore(STGM_READ, out var store);
            var k = new PROPKEY { fmtid = FriendlyName_Key, pid = FriendlyName_PID };
            store.GetValue(ref k, out var pv);
            if (pv.vt == 31) return Marshal.PtrToStringUni(pv.ptr); // 31 = VT_LPWSTR
        }
        catch (COMException) { }
        return null;
    }

    public static string? DefaultOutputDeviceName()
    {
        CoInitializeEx(IntPtr.Zero, 0);
        try {
            var c = MMDevEnum_CLSID; var i = MMDevEnum_IID;
            if (CoCreateInstance(ref c, IntPtr.Zero, CLSCTX_ALL, ref i, out var raw) != S_OK) return null;
            var e = (IMMDeviceEnumerator)raw;
            var d = GetDefault(e);
            var name = GetFriendlyName(d);
            Marshal.ReleaseComObject(d);
            Marshal.ReleaseComObject(e);
            return name;
        }
        finally { CoUninitialize(); }
    }

    public static List<string> ListOutputDevices()
    {
        var result = new List<string>();
        CoInitializeEx(IntPtr.Zero, 0);
        try {
            var c = MMDevEnum_CLSID; var i = MMDevEnum_IID;
            if (CoCreateInstance(ref c, IntPtr.Zero, CLSCTX_ALL, ref i, out var raw) != S_OK) return result;
            var e = (IMMDeviceEnumerator)raw;
            e.EnumAudioEndpoints(eRender, 1, out var col);
            col.GetCount(out uint n);
            for (uint j = 0; j < n; j++) {
                col.Item(j, out var dev);
                var name = GetFriendlyName(dev);
                if (name != null) result.Add(name);
                Marshal.ReleaseComObject(dev);
            }
            Marshal.ReleaseComObject(e);
        }
        finally { CoUninitialize(); }
        return result;
    }

    public void Dispose() => Stop();

    [DllImport("ole32.dll")] static extern int  CoInitializeEx(IntPtr r, int apt);
    [DllImport("ole32.dll")] static extern void CoUninitialize();
    [DllImport("ole32.dll")] static extern int  CoCreateInstance(ref Guid cls, IntPtr outer, int ctx, ref Guid iid,
        [MarshalAs(UnmanagedType.IUnknown)] out object ppv);

    [ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDeviceEnumerator {
        [PreserveSig] int EnumAudioEndpoints(int flow, int mask, [MarshalAs(UnmanagedType.Interface)] out IMMDeviceCollection col);
        [PreserveSig] int GetDefaultAudioEndpoint(int flow, int role, [MarshalAs(UnmanagedType.Interface)] out IMMDevice dev);
        [PreserveSig] int GetDevice([MarshalAs(UnmanagedType.LPWStr)] string id, [MarshalAs(UnmanagedType.Interface)] out IMMDevice dev);
        [PreserveSig] int RegisterEndpointNotificationCallback(IntPtr c);
        [PreserveSig] int UnregisterEndpointNotificationCallback(IntPtr c);
    }
    [ComImport, Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDeviceCollection {
        [PreserveSig] int GetCount(out uint n);
        [PreserveSig] int Item(uint i, [MarshalAs(UnmanagedType.Interface)] out IMMDevice dev);
    }
    [ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDevice {
        [PreserveSig] int Activate(ref Guid iid, int ctx, IntPtr p, [MarshalAs(UnmanagedType.IUnknown)] out object iface);
        [PreserveSig] int OpenPropertyStore(int mode, [MarshalAs(UnmanagedType.Interface)] out IPropertyStore store);
        [PreserveSig] int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
        [PreserveSig] int GetState(out uint state);
    }
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPropertyStore {
        [PreserveSig] int GetCount(out uint n);
        [PreserveSig] int GetAt(uint i, out PROPKEY key);
        [PreserveSig] int GetValue(ref PROPKEY key, out PROPVAR val);
        [PreserveSig] int SetValue(ref PROPKEY key, ref PROPVAR val);
        [PreserveSig] int Commit();
    }
    [ComImport, Guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioClient {
        [PreserveSig] int Initialize(int mode, uint flags, long dur, long period, ref WAVEFORMATEX fmt, IntPtr sid);
        [PreserveSig] int GetBufferSize(out uint n);
        [PreserveSig] int GetStreamLatency(out long lat);
        [PreserveSig] int GetCurrentPadding(out uint pad);
        [PreserveSig] int IsFormatSupported(int mode, ref WAVEFORMATEX fmt, IntPtr closest);
        [PreserveSig] int GetMixFormat(out IntPtr fmt);
        [PreserveSig] int GetDevicePeriod(out long def, out long min);
        [PreserveSig] int Start();
        [PreserveSig] int Stop();
        [PreserveSig] int Reset();
        [PreserveSig] int SetEventHandle(IntPtr h);
        [PreserveSig] int GetService(ref Guid iid, [MarshalAs(UnmanagedType.IUnknown)] out object svc);
    }
    [ComImport, Guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioCaptureClient {
        [PreserveSig] int GetBuffer(out IntPtr data, out uint frames, out uint flags, out ulong devPos, out ulong qpcPos);
        [PreserveSig] int ReleaseBuffer(uint n);
        [PreserveSig] int GetNextPacketSize(out uint n);
    }

    [StructLayout(LayoutKind.Sequential, Pack = 2)]
    struct WAVEFORMATEX {
        public ushort wFormatTag, nChannels;
        public uint   nSamplesPerSec, nAvgBytesPerSec;
        public ushort nBlockAlign, wBitsPerSample, cbSize;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct PROPKEY { public Guid fmtid; public uint pid; }
    [StructLayout(LayoutKind.Explicit, Size = 16)]
    struct PROPVAR  { [FieldOffset(0)] public ushort vt; [FieldOffset(8)] public IntPtr ptr; }
}

public class ScreenGrabber
{
    readonly Rectangle captureRect;
    readonly Size outputSize;
    readonly int targetFps;
    readonly Action<byte[], double> frameCallback;
    readonly CancellationToken cancel;

    // 78 gives decent quality without the file sizes ballooning
    // tried 85 but the ring buffer would OOM after a few minutes at 1080p
    const int JpegQuality = 78;

    static readonly ImageCodecInfo JpegEncoder = ImageCodecInfo.GetImageEncoders()
        .First(c => c.FormatID == ImageFormat.Jpeg.Guid);

    public int DroppedFrames { get; private set; }
    public double ActualFps  { get; private set; }

    public ScreenGrabber(Rectangle region, Size outSize, int fps, Action<byte[], double> onFrame, CancellationToken ct)
    {
        captureRect   = region;
        outputSize    = outSize;
        targetFps     = fps;
        frameCallback = onFrame;
        cancel        = ct;
    }

    public void Run()
    {
        var encParams = new EncoderParameters(1);
        encParams.Param[0] = new EncoderParameter(Encoder.Quality, (long)JpegQuality);
        bool needsScale = outputSize.Width != captureRect.Width || outputSize.Height != captureRect.Height;

        using var bmp = new Bitmap(captureRect.Width, captureRect.Height, PixelFormat.Format32bppArgb);
        using var gfx = Graphics.FromImage(bmp);

        var interval = TimeSpan.FromSeconds(1.0 / targetFps);
        var nextTick = DateTime.UtcNow;
        int frameCount = 0;
        var fpsTimer   = DateTime.UtcNow;

        while (!cancel.IsCancellationRequested)
        {
            double ts = (DateTime.UtcNow - DateTime.UnixEpoch).TotalSeconds;

            try { gfx.CopyFromScreen(captureRect.Location, Point.Empty, captureRect.Size); }
            catch (System.ComponentModel.Win32Exception)
            {
                // screen probably locked or on the secure desktop — skip and keep trying
                DroppedFrames++;
                Thread.Sleep(33);
                continue;
            }

            Bitmap toEncode = bmp;
            Bitmap? scaled  = null;
            if (needsScale) { scaled = new Bitmap(bmp, outputSize); toEncode = scaled; }

            using var ms = new MemoryStream();
            toEncode.Save(ms, JpegEncoder, encParams);
            frameCallback(ms.ToArray(), ts);
            scaled?.Dispose();

            frameCount++;
            if ((DateTime.UtcNow - fpsTimer).TotalSeconds >= 1.0) {
                ActualFps = frameCount;
                frameCount = 0;
                fpsTimer = DateTime.UtcNow;
            }

            nextTick += interval;
            var delay = nextTick - DateTime.UtcNow;
            if (delay > TimeSpan.Zero) Thread.Sleep(delay);
            else DroppedFrames++;
        }
    }

    // scales to fit target height while keeping aspect ratio
    // & ~1 forces even dimensions — h264 encoders reject odd widths/heights
    public static Size ScaleToHeight(Rectangle monitor, string label)
    {
        int targetH = label switch { "360p" => 360, "720p" => 720, _ => 1080 };
        if (monitor.Height <= targetH) return monitor.Size;
        double ratio = (double)targetH / monitor.Height;
        return new Size((int)(monitor.Width * ratio) & ~1, (int)(monitor.Height * ratio) & ~1);
    }
}

static class ClipEncoder
{
    public static void WriteClip(
        IList<(double ts, byte[] jpg)> videoFrames,
        IList<(double ts, byte[] pcm)> audioChunks,
        WavFormat? audioFmt,
        int fps, string outputDir, string ffmpegPath,
        Action<bool, string> onFinished)
    {
        if (videoFrames.Count == 0) {
            onFinished(false, "No frames buffered yet — wait a moment after startup");
            return;
        }

        // rough space estimate: ~5MB/s for 1080p30, 0.5MB/s audio — warn if tight
        try {
            var drive = new DriveInfo(Path.GetPathRoot(outputDir) ?? "C:\\");
            int clipSecs = videoFrames.Count > 1
                ? (int)(videoFrames[^1].ts - videoFrames[0].ts) + 1
                : 30;
            long estimatedBytes = (long)(clipSecs * (fps >= 30 ? 5_500_000L : 2_800_000L));
            if (drive.AvailableFreeSpace < estimatedBytes * 2)
                System.Diagnostics.Debug.WriteLine($"low disk space warning: {drive.AvailableFreeSpace / 1_000_000}MB free");
        }
        catch (Exception) { /* non-fatal, just a heads-up */ }

        try { Directory.CreateDirectory(outputDir); }
        catch (UnauthorizedAccessException ex) {
            onFinished(false, $"Can't write to {outputDir}: {ex.Message}");
            return;
        }

        string stamp   = DateTime.Now.ToString("yyyyMMdd_HHmmss");
        string wavTmp  = Path.Combine(outputDir, $".tmp_audio_{stamp}.wav");
        string outFile = Path.Combine(outputDir, $"clip_{stamp}.mp4");

        bool withAudio = audioChunks.Count > 0 && audioFmt != null;
        if (withAudio) WriteWav(wavTmp, audioChunks, audioFmt!);

        // recalculate actual fps from timestamps — drops mess up the declared framerate
        double realFps = fps;
        if (videoFrames.Count >= 2) {
            double span = videoFrames[^1].ts - videoFrames[0].ts;
            if (span > 0.5) realFps = Math.Max(1.0, (videoFrames.Count - 1) / span);
        }

        var args = new List<string> {
            "-y", "-f", "image2pipe",
            "-framerate", realFps.ToString("F4"),
            "-i", "pipe:0"
        };
        if (withAudio) { args.Add("-i"); args.Add($"\"{wavTmp}\""); }
        args.AddRange(["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-vf", $"fps={fps}"]);
        if (withAudio) args.AddRange(["-c:a", "aac", "-b:a", "192k", "-shortest"]);
        args.Add($"\"{outFile}\"");

        var psi = new ProcessStartInfo(ffmpegPath, string.Join(" ", args)) {
            UseShellExecute       = false,
            RedirectStandardInput = true,
            RedirectStandardError = true,
            CreateNoWindow        = true,
        };

        Process proc;
        try { proc = Process.Start(psi)!; }
        catch (Exception ex) { onFinished(false, "ffmpeg wouldn't start: " + ex.Message); return; }

        using (proc)
        {
            var stderrReader = proc.StandardError.ReadToEndAsync();

            var feeder = new Thread(() => {
                try {
                    foreach (var f in videoFrames)
                        proc.StandardInput.BaseStream.Write(f.jpg);
                }
                catch (IOException) { /* ffmpeg closed the pipe early, probably ran out of disk */ }
                finally {
                    try { proc.StandardInput.Close(); } catch { }
                }
            }) { IsBackground = true };
            feeder.Start();

            string stderr = stderrReader.GetAwaiter().GetResult();
            proc.WaitForExit();
            feeder.Join(2000);

            if (withAudio) { try { File.Delete(wavTmp); } catch { } }

            if (proc.ExitCode == 0)
            {
                // include file size in the result so the UI can show it
                string sizeStr = "";
                try {
                    long bytes = new FileInfo(outFile).Length;
                    sizeStr = bytes > 1_000_000 ? $" ({bytes / 1_000_000.0:F1}MB)" : $" ({bytes / 1000}KB)";
                }
                catch { }
                onFinished(true, outFile + sizeStr);
            }
            else
            {
                // ffmpeg error output tends to be long, grab the last 300 chars where the actual error usually is
                string errSnip = stderr.Length > 300 ? "…" + stderr[^300..] : stderr;
                onFinished(false, errSnip);
            }
        }
    }

    static void WriteWav(string path, IList<(double, byte[])> chunks, WavFormat fmt)
    {
        int totalBytes = chunks.Sum(c => c.Item2.Length);
        using var fs = new FileStream(path, FileMode.Create);
        using var bw = new BinaryWriter(fs);
        bw.Write("RIFF"u8.ToArray());
        bw.Write(36 + totalBytes);
        bw.Write("WAVE"u8.ToArray());
        bw.Write("fmt "u8.ToArray());
        bw.Write(16);
        bw.Write(fmt.Tag); bw.Write(fmt.Channels);
        bw.Write(fmt.SampleRate); bw.Write(fmt.BytesPerSec);
        bw.Write(fmt.BlockAlign); bw.Write(fmt.BitDepth);
        bw.Write("data"u8.ToArray());
        bw.Write(totalBytes);
        foreach (var (_, pcm) in chunks) bw.Write(pcm);
    }
}

public class MainForm : Form
{
    static readonly Color BG      = Color.FromArgb(0x12, 0x12, 0x12);
    static readonly Color DIV     = Color.FromArgb(0x26, 0x26, 0x26);
    static readonly Color ACCENT  = Color.FromArgb(0xB0, 0xB0, 0xB0);
    static readonly Color FG      = Color.FromArgb(0xE8, 0xE8, 0xE8);
    static readonly Color FG2     = Color.FromArgb(0x60, 0x60, 0x60);
    static readonly Color FG3     = Color.FromArgb(0x44, 0x44, 0x44);
    static readonly Color TBAR    = Color.FromArgb(0x09, 0x09, 0x09);
    static readonly Color REDDOT  = Color.FromArgb(0xF0, 0x40, 0x40);
    static readonly Font  F9      = new("Segoe UI", 9);
    static readonly Font  F9B     = new("Segoe UI", 9, FontStyle.Bold);
    static readonly Font  F8      = new("Segoe UI", 8);
    static readonly Font  F7      = new("Segoe UI", 7);

    const int WM_HOTKEY = 0x0312, WM_NCHITTEST = 0x0084;
    const int HTCAPTION = 2, HTCLIENT = 1;
    const int HK = 1; // hotkey ID — only ever register one
    const uint CTRL = 0x0002, ALT = 0x0001, SHIFT = 0x0004, WIN = 0x0008;

    [DllImport("user32.dll")] static extern bool RegisterHotKey(IntPtr hwnd, int id, uint mods, uint vk);
    [DllImport("user32.dll")] static extern bool UnregisterHotKey(IntPtr hwnd, int id);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr hwnd, int nCmdShow);
    [DllImport("dwmapi.dll")] static extern int  DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int val, int size);
    [DllImport("winmm.dll",  CharSet = CharSet.Unicode)]
    static extern int mciSendString(string cmd, char[]? ret, int cch, IntPtr hwnd);

    readonly RingBuffer<byte[]> vidBuf = new(), audBuf = new();
    WavFormat?       captureFormat;
    LoopbackRecorder? audioRec;
    CancellationTokenSource? recCts;
    DateTime         recStarted;
    string?          lastClipPath, ffmpegExe;
    bool             saving;
    ClipperConfig    cfg;
    List<DisplayInfo> monitors = [];
    List<string>      audioDevs = [];

    static readonly Dictionary<string, int> DurationMap = new() {
        ["5 sec"]  = 5,
        ["10 sec"] = 10,
        ["15 sec"] = 15,
        ["30 sec"] = 30,
        ["1 min"]  = 60,
    };

    // ui refs
    Panel       recDot    = null!;
    bool        dotBlink;
    Label       timeLabel = null!, statusLabel = null!;
    ThinBar     progressBar = null!;
    DropPicker  monPicker = null!, resPicker = null!, fpsPicker = null!,
                audPicker = null!, durPicker = null!;
    StyledTextBox hotkeyInput = null!, saveDirInput = null!;
    CheckBox    alwaysOnTop = null!;
    RoundButton previewBtn  = null!;
    System.Windows.Forms.Timer blinkTimer = null!, tickTimer = null!;
    System.Windows.Forms.Timer? pendingRestart;
    Point dragStart;

    const int FORM_W = 400, PAD = 22, ROW_H = 34, LABEL_W = 96;
    Panel root = null!;
    int   layoutY;

    public MainForm()
    {
        cfg = ClipperConfig.Load();
        FormBorderStyle = FormBorderStyle.None;
        BackColor       = Color.FromArgb(0x28, 0x28, 0x28);
        base.Text       = "Clipper";
        StartPosition   = FormStartPosition.CenterScreen;
        TopMost         = cfg.Topmost;
        SuspendLayout();
        Build();
        ResumeLayout(true);
        Load        += OnFormLoad;
        FormClosing += (_, _) => Shutdown();
    }

    void OnFormLoad(object? s, EventArgs e)
    {
        try
        {
            // rounded corners on win11 — attribute 33 = DWMWA_WINDOW_CORNER_PREFERENCE
            // value 2 = DWMWCP_ROUND, silently fails on win10 which is fine
            try { int v = 2; DwmSetWindowAttribute(Handle, 33, ref v, sizeof(int)); } catch { }

            ffmpegExe = FfmpegFinder.Locate();
            monitors  = DisplayHelper.Enumerate();
            audioDevs = LoopbackRecorder.ListOutputDevices();
            PopulateAllDropdowns();

            blinkTimer = new System.Windows.Forms.Timer { Interval = 600 };
            blinkTimer.Tick += (_, _) => { dotBlink = !dotBlink; recDot.Invalidate(); };
            blinkTimer.Start();

            tickTimer = new System.Windows.Forms.Timer { Interval = 500 };
            tickTimer.Tick += (_, _) => RefreshTimecode();

            if (ffmpegExe == null)
                statusLabel.Text = "ffmpeg not found — drop ffmpeg.exe next to Clipper.exe or add it to PATH";
            else
                BeginInvoke(StartCapture);
        }
        catch (Exception ex) { MessageBox.Show(ex.ToString(), "Startup error"); }
    }

    void Build()
    {
        root = new Panel { Location = new Point(1, 1), BackColor = BG };
        Controls.Add(root);

        void Rim(int x, int y, int w, int h) =>
            Controls.Add(new Panel {
                Location  = new Point(x, y),
                Size      = new Size(w, h),
                BackColor = Color.FromArgb(0x28, 0x28, 0x28)
            });

        layoutY = 0;
        TitleBar();
        Separator();
        RecStrip();
        Separator();
        SectionHead("CAPTURE");
        DropRow("Monitor",     ref monPicker, []);
        DropRow("Resolution",  ref resPicker, ["360p", "720p", "1080p"]);
        DropRow("FPS",         ref fpsPicker, ["15", "30", "60"]);
        DropRow("Audio",       ref audPicker, []);
        DropRow("Clip length", ref durPicker, [.. DurationMap.Keys]);
        Separator();
        SectionHead("OUTPUT");
        InputRow("Hotkey",  out hotkeyInput,  cfg.Hotkey);
        hotkeyInput.Leave   += (_, _) => ScheduleRestart();
        hotkeyInput.KeyDown += (_, ev) => { if (ev.KeyCode == Keys.Return) ScheduleRestart(); };
        SaveDirRow();
        Separator();
        BottomBar();

        layoutY += 14;
        root.Size  = new Size(FORM_W, layoutY);
        ClientSize = new Size(FORM_W + 2, layoutY + 2);

        Rim(0, 0, FORM_W + 2, 1);
        Rim(0, layoutY + 1, FORM_W + 2, 1);
        Rim(0, 0, 1, layoutY + 2);
        Rim(FORM_W + 1, 0, 1, layoutY + 2);
    }

    void TitleBar()
    {
        root.Controls.Add(new Panel { Location = Point.Empty, Size = new Size(FORM_W, 2), BackColor = ACCENT });
        layoutY += 2;

        var bar = new Panel { Location = new Point(0, layoutY), Size = new Size(FORM_W, 38), BackColor = TBAR };
        root.Controls.Add(bar);
        bar.Controls.Add(new Label {
            Text = "Clipper", ForeColor = FG, Font = F9B,
            Location = new Point(PAD, 0), Size = new Size(120, 38),
            TextAlign = ContentAlignment.MiddleLeft, BackColor = TBAR
        });

        var closeBtn = TBarLabel("×");
        var minBtn   = TBarLabel("—");
        closeBtn.Left = FORM_W - 38;
        minBtn.Left   = FORM_W - 76;
        bar.Controls.AddRange([closeBtn, minBtn]);
        bar.Resize += (_, _) => { closeBtn.Left = bar.Width - 38; minBtn.Left = bar.Width - 76; };

        closeBtn.Click      += (_, _) => Close();
        closeBtn.MouseEnter += (_, _) => closeBtn.BackColor = Color.FromArgb(0xC4, 0x2B, 0x1D);
        closeBtn.MouseLeave += (_, _) => closeBtn.BackColor = TBAR;
        minBtn.Click        += (_, _) => ShowWindow(Handle, 6); // SW_MINIMIZE
        minBtn.MouseEnter   += (_, _) => minBtn.BackColor = Color.FromArgb(0x26, 0x26, 0x26);
        minBtn.MouseLeave   += (_, _) => minBtn.BackColor = TBAR;

        AttachDrag(bar, [closeBtn, minBtn]);
        layoutY += 38;
    }

    static Label TBarLabel(string ch) => new() {
        Text = ch, ForeColor = FG2, Font = F9, BackColor = TBAR,
        Size = new Size(38, 38), TextAlign = ContentAlignment.MiddleCenter,
        Cursor = Cursors.Hand, Top = 0,
    };

    void RecStrip()
    {
        layoutY += 14;
        var row = new Panel { Location = new Point(PAD, layoutY), Size = new Size(FORM_W - PAD * 2, 16), BackColor = BG };
        root.Controls.Add(row);

        recDot = new Panel { Location = new Point(0, 4), Size = new Size(8, 8), BackColor = BG };
        recDot.Paint += (_, ev) => {
            ev.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            ev.Graphics.FillEllipse(
                new SolidBrush(dotBlink ? REDDOT : Color.FromArgb(0x40, 0x40, 0x40)),
                0, 0, 8, 8);
        };
        row.Controls.Add(recDot);
        row.Controls.Add(new Label { Text = "REC", ForeColor = FG3, Font = F8, Location = new Point(16, 0), AutoSize = true, BackColor = BG });

        timeLabel = new Label { Text = "", ForeColor = FG2, Font = F8, AutoSize = true, BackColor = BG };
        timeLabel.Location = new Point(row.Width - timeLabel.PreferredWidth, 0);
        row.Controls.Add(timeLabel);
        layoutY += 20;

        progressBar = new ThinBar { Location = new Point(PAD, layoutY), Size = new Size(FORM_W - PAD * 2, 2) };
        root.Controls.Add(progressBar);
        layoutY += 14;
    }

    void SectionHead(string label)
    {
        layoutY += 10;
        root.Controls.Add(new Label {
            Text = label, ForeColor = FG3, Font = F7,
            Location = new Point(PAD, layoutY), AutoSize = true, BackColor = BG
        });
        layoutY += 18;
    }

    void DropRow(string labelText, ref DropPicker picker, string[] opts)
    {
        int ctrlW = FORM_W - PAD * 2 - LABEL_W - 8;
        AddRowLabel(labelText);
        var dp = new DropPicker {
            Location = new Point(PAD + LABEL_W + 8, layoutY - ROW_H + (ROW_H - 26) / 2),
            Size     = new Size(ctrlW, 26),
        };
        if (opts.Length > 0) dp.SetItems(opts);
        dp.SelectedIndexChanged += (_, _) => ScheduleRestart();
        root.Controls.Add(dp);
        picker = dp;
    }

    void InputRow(string labelText, out StyledTextBox box, string initialValue)
    {
        int ctrlW = FORM_W - PAD * 2 - LABEL_W - 8;
        AddRowLabel(labelText);
        var tb = new StyledTextBox(initialValue) {
            Location = new Point(PAD + LABEL_W + 8, layoutY - ROW_H + (ROW_H - 26) / 2),
            Size     = new Size(ctrlW, 26),
        };
        root.Controls.Add(tb);
        box = tb;
    }

    void SaveDirRow()
    {
        int btnW  = 64;
        int ctrlW = FORM_W - PAD * 2 - LABEL_W - 8;
        AddRowLabel("Save to");
        saveDirInput = new StyledTextBox(cfg.OutDir) {
            Location = new Point(PAD + LABEL_W + 8, layoutY - ROW_H + (ROW_H - 26) / 2),
            Size     = new Size(ctrlW - btnW - 6, 26),
        };
        saveDirInput.Leave   += (_, _) => ScheduleRestart();
        saveDirInput.KeyDown += (_, ev) => { if (ev.KeyCode == Keys.Return) ScheduleRestart(); };
        root.Controls.Add(saveDirInput);
        root.Controls.Add(new RoundButton("Browse", BrowseForFolder, btnW, 26) {
            Location = new Point(PAD + LABEL_W + 8 + ctrlW - btnW, layoutY - ROW_H + (ROW_H - 26) / 2)
        });
    }

    void AddRowLabel(string text)
    {
        root.Controls.Add(new Label {
            Text = text, ForeColor = FG2, Font = F9,
            Location = new Point(PAD, layoutY + (ROW_H - 16) / 2),
            Size = new Size(LABEL_W, 16),
        });
        layoutY += ROW_H;
    }

    void BottomBar()
    {
        layoutY += 14;

        previewBtn = new RoundButton("▶  Preview", OpenLastClip, 104, 30) {
            Location = new Point(PAD, layoutY),
            Enabled  = false,
        };
        root.Controls.Add(previewBtn);

        root.Controls.Add(new RoundButton("Open folder", OpenOutputDir, 104, 30) {
            Location = new Point(PAD + 104 + 8, layoutY)
        });

        alwaysOnTop = new CheckBox {
            Text      = "On top",
            Checked   = cfg.Topmost,
            ForeColor = FG2,
            BackColor = BG,
            Font      = F8,
            AutoSize  = true,
            FlatStyle = FlatStyle.Flat,
            Location  = new Point(PAD + 104 + 8 + 104 + 10, layoutY + 7),
        };
        alwaysOnTop.FlatAppearance.BorderSize       = 0;
        alwaysOnTop.FlatAppearance.CheckedBackColor = Color.Transparent;
        alwaysOnTop.CheckedChanged += (_, _) => TopMost = alwaysOnTop.Checked;
        root.Controls.Add(alwaysOnTop);
        layoutY += 38;

        statusLabel = new Label {
            Text      = "Starting…",
            ForeColor = FG3,
            Font      = F8,
            Location  = new Point(PAD, layoutY),
            Size      = new Size(FORM_W - PAD * 2, 16),
        };
        root.Controls.Add(statusLabel);
        layoutY += 18;
    }

    void Separator()
    {
        root.Controls.Add(new Panel { Location = new Point(0, layoutY), Size = new Size(FORM_W, 1), BackColor = DIV });
        layoutY += 1;
    }

    void AttachDrag(Control ctrl, Control[]? excluded = null)
    {
        if (excluded != null && Array.IndexOf(excluded, ctrl) >= 0) return;
        ctrl.MouseDown += (_, e) => { if (e.Button == MouseButtons.Left) dragStart = Cursor.Position; };
        ctrl.MouseMove += (_, e) => {
            if (e.Button != MouseButtons.Left) return;
            var cur = Cursor.Position;
            Left += cur.X - dragStart.X;
            Top  += cur.Y - dragStart.Y;
            dragStart = cur;
        };
        foreach (Control child in ctrl.Controls) AttachDrag(child, excluded);
    }

    void PopulateAllDropdowns()
    {
        monPicker.ClearItems();
        monPicker.SetItems(monitors.Select(m => m.ToString()).ToArray());
        if (monitors.Count > 0)
            monPicker.SelectedIndex = Math.Min(cfg.MonitorIdx, monitors.Count - 1);

        resPicker.SelectedItem = cfg.Res;
        fpsPicker.SelectedItem = cfg.Fps.ToString();

        audPicker.ClearItems();
        var defDev   = LoopbackRecorder.DefaultOutputDeviceName();
        string defLabel = defDev != null ? $"Default — {defDev}" : "Default";
        audPicker.SetItems(new[] { defLabel }.Concat(audioDevs).ToArray());
        audPicker.SelectedIndex = cfg.AudioDevice == null
            ? 0
            : Math.Max(0, audioDevs.IndexOf(cfg.AudioDevice) + 1);

        durPicker.SelectedItem = cfg.ClipLen;
        if (durPicker.SelectedIndex < 0) durPicker.SelectedIndex = 3; // default to 30 sec
    }

    void ScheduleRestart()
    {
        pendingRestart?.Stop();
        pendingRestart?.Dispose();
        pendingRestart = new System.Windows.Forms.Timer { Interval = 500 };
        pendingRestart.Tick += (_, _) => {
            pendingRestart!.Stop();
            SaveAndRestart();
        };
        pendingRestart.Start();
    }

    void SaveAndRestart()
    {
        PersistSettings();
        Shutdown();
        progressBar.SetProgress(0, 1);
        statusLabel.Text = "Restarting capture…";
        var t = new System.Windows.Forms.Timer { Interval = 300 };
        t.Tick += (_, _) => { t.Stop(); t.Dispose(); StartCapture(); };
        t.Start();
    }

    void PersistSettings()
    {
        cfg.MonitorIdx   = Math.Max(0, monPicker.SelectedIndex);
        cfg.Res          = resPicker.SelectedItem?.ToString() ?? "1080p";
        cfg.Fps          = int.TryParse(fpsPicker.SelectedItem?.ToString(), out int f) ? f : 30;
        int ai           = audPicker.SelectedIndex;
        cfg.AudioDevice  = ai > 0 && ai <= audioDevs.Count ? audioDevs[ai - 1] : null;
        cfg.ClipLen      = durPicker.SelectedItem?.ToString() ?? "30 sec";
        cfg.Hotkey       = hotkeyInput.Text.Trim();
        cfg.OutDir       = saveDirInput.Text;
        cfg.Topmost      = alwaysOnTop.Checked;
        cfg.Persist();
    }

    void StartCapture()
    {
        recCts?.Cancel();
        recCts = new CancellationTokenSource();
        var tok = recCts.Token;

        int monIdx  = Math.Max(0, monPicker.SelectedIndex);
        var monitor = monIdx < monitors.Count ? monitors[monIdx] : monitors[0];
        int fps     = int.TryParse(fpsPicker.SelectedItem?.ToString(), out int fv) ? fv : 30;
        var outSz   = ScreenGrabber.ScaleToHeight(monitor.Rect, resPicker.SelectedItem?.ToString() ?? "1080p");
        int bufSecs = (DurationMap.TryGetValue(cfg.ClipLen, out int d) ? d : 30) + 2;

        vidBuf.WindowSecs = bufSecs; audBuf.WindowSecs = bufSecs;
        vidBuf.Clear(); audBuf.Clear();
        captureFormat = null;

        int audIdx = audPicker.SelectedIndex;
        string? devName = audIdx > 0 && audIdx <= audioDevs.Count ? audioDevs[audIdx - 1] : null;

        audioRec?.Dispose();
        audioRec = new LoopbackRecorder(devName, (data, n) => {
            if (tok.IsCancellationRequested) return;
            var copy = new byte[n];
            Buffer.BlockCopy(data, 0, copy, 0, n);
            audBuf.Add((DateTime.UtcNow - DateTime.UnixEpoch).TotalSeconds, copy);
        });

        bool audioStarted = audioRec.Start();
        if (!audioStarted)
            statusLabel.Text = "Audio device not responding — capturing video only";
        captureFormat = audioRec.CaptureFormat;

        Task.Run(new ScreenGrabber(monitor.Rect, outSz, fps,
            (jpg, ts) => vidBuf.Add(ts, jpg), tok).Run, tok);

        RegisterHotkeyFromString(cfg.Hotkey);
        recStarted = DateTime.UtcNow;

        if (audioStarted)
            statusLabel.Text = $"Buffering  ·  {cfg.Hotkey}  saves the last {cfg.ClipLen}";
        tickTimer.Start();
    }

    void Shutdown()
    {
        tickTimer?.Stop();
        recCts?.Cancel();
        audioRec?.Stop();
        audioRec?.Dispose();
        audioRec = null;
        UnregisterHotKey(Handle, HK);
    }

    void RegisterHotkeyFromString(string hk)
    {
        UnregisterHotKey(Handle, HK);
        uint mods = 0, vk = 0;
        foreach (var part in hk.ToLower().Split('+').Select(p => p.Trim()))
        {
            switch (part)
            {
                case "ctrl":  mods |= CTRL;  break;
                case "alt":   mods |= ALT;   break;
                case "shift": mods |= SHIFT; break;
                case "win":   mods |= WIN;   break;
                default:
                    if (part.Length == 1)
                        vk = (uint)char.ToUpper(part[0]);
                    else if (part.StartsWith("f") && int.TryParse(part[1..], out int fn) && fn is >= 1 and <= 24)
                        vk = (uint)(Keys.F1 + fn - 1);
                    break;
            }
        }
        if (vk != 0) RegisterHotKey(Handle, HK, mods, vk);
    }

    void RefreshTimecode()
    {
        if (recCts == null || recCts.IsCancellationRequested) return;
        int cap = DurationMap.TryGetValue(cfg.ClipLen, out int d) ? d : 30;
        double elapsed = Math.Min((DateTime.UtcNow - recStarted).TotalSeconds, cap);
        progressBar.SetProgress(elapsed, cap);
        timeLabel.Text = $"{(int)elapsed}s / {cap}s";

        // brief "warming up" indicator while the buffer fills
        if (elapsed < 2 && statusLabel.Text.StartsWith("Buffering"))
            statusLabel.Text = "Warming up…";
        else if (elapsed >= 2 && statusLabel.Text == "Warming up…")
            statusLabel.Text = $"Buffering  ·  {cfg.Hotkey}  saves the last {cfg.ClipLen}";
    }

    protected override void WndProc(ref Message m)
    {
        if (m.Msg == WM_HOTKEY && m.WParam.ToInt32() == HK)
        {
            BeginInvoke(TrySaveClip);
            return;
        }
        if (m.Msg == WM_NCHITTEST)
        {
            base.WndProc(ref m);
            if (m.Result.ToInt32() == HTCLIENT)
            {
                var p = PointToClient(Cursor.Position);
                if (p.Y >= 2 && p.Y < 42 && p.X < Width - 76)
                    m.Result = new IntPtr(HTCAPTION);
            }
            return;
        }
        base.WndProc(ref m);
    }

    void TrySaveClip()
    {
        if (saving || ffmpegExe == null) return;
        saving = true;
        statusLabel.Text = "Saving clip…";

        int clipSecs = DurationMap.TryGetValue(cfg.ClipLen, out int d) ? d : 30;
        int fps      = int.TryParse(fpsPicker.SelectedItem?.ToString(), out int fv) ? fv : 30;
        double from  = (DateTime.UtcNow - DateTime.UnixEpoch).TotalSeconds - clipSecs;

        var frames = vidBuf.Since(from).Select(x => (x.when, x.payload)).ToList();
        var audio  = audBuf.Since(from).Select(x => (x.when, x.payload)).ToList();

        var fmt  = captureFormat;
        string ff = ffmpegExe!;
        string dir = cfg.OutDir;

        Task.Run(() => ClipEncoder.WriteClip(frames, audio, fmt, fps, dir, ff,
            (ok, info) => BeginInvoke(() => OnSaveComplete(ok, info, frames.Count))));
    }

    void OnSaveComplete(bool success, string info, int frameCount)
    {
        saving = false;
        if (success)
        {
            // info = "path/clip_xxx.mp4 (4.2MB)" — split out the path part
            string path = info.Contains(' ') ? info[..info.IndexOf(' ')] : info;
            string meta = info.Contains(' ') ? info[info.IndexOf(' ')..].Trim() : "";
            lastClipPath = path;
            previewBtn.Enabled = true;

            int drops = audioRec?.LatePackets ?? 0;
            string dropNote = drops > 0 ? $"  ·  {drops} late audio pkts" : "";
            statusLabel.Text = $"Saved {Path.GetFileName(path)} {meta}{dropNote}";
            Task.Run(PlaySaveChime);
        }
        else
        {
            statusLabel.Text = $"Failed: {info[..Math.Min(info.Length, 70)]}";
        }
    }

    void OpenLastClip()
    {
        if (lastClipPath == null || !File.Exists(lastClipPath)) return;
        Process.Start(new ProcessStartInfo(lastClipPath) { UseShellExecute = true });
    }

    void OpenOutputDir()
    {
        try { Directory.CreateDirectory(saveDirInput.Text); } catch { }
        Process.Start(new ProcessStartInfo("explorer", saveDirInput.Text) { UseShellExecute = true });
    }

    void BrowseForFolder()
    {
        using var dlg = new FolderBrowserDialog { SelectedPath = saveDirInput.Text };
        if (dlg.ShowDialog() == DialogResult.OK)
            saveDirInput.Text = dlg.SelectedPath;
    }

    protected override void OnFormClosed(FormClosedEventArgs e)
    {
        Shutdown();
        blinkTimer?.Dispose();
        tickTimer?.Dispose();
        pendingRestart?.Dispose();
        base.OnFormClosed(e);
    }

    static void PlaySaveChime()
    {
        try
        {
            var mp3 = Convert.FromBase64String(ChimeData);
            var tmp = Path.GetTempFileName() + ".mp3";
            File.WriteAllBytes(tmp, mp3);
            mciSendString($"open \"{tmp}\" type mpegvideo alias chime", null, 0, IntPtr.Zero);
            mciSendString("play chime wait", null, 0, IntPtr.Zero);
            mciSendString("close chime", null, 0, IntPtr.Zero);
            try { File.Delete(tmp); } catch (IOException) { }
        }
        catch (Exception) { }
    }

    const string ChimeData =
        "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4xMDCqqqqqTEFNRTMuMTAwqqqqqkxBTUUzLjEwMKqqqqpMQU1FMy4xMDCqqqqqTEFNRTMuMTAwqqqqqkxBTUUzLjEwMKqqqqpMQU1FMy4xMDCqqqqqTEFNRTMuMTAwqqqqqkxBTUUzLjEwMKqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqr/8xDE8AAAA0gBwAAAqqqqqqqqqqqqqqqqqv/zEMTyAAADSAAAAACqqqqqqqqqqqqqqqqq//MQxPIAAANIAAAAAKqqqqqqqqqqqqqqqqr/8xDE8gAAA0gAAAAAqqqqqqqqqqqqqqqqqv/zEMTyAAADSAAAAACqqqqqqqqqqqqqqqqq//MQxPIAAANIAAAAAKqqqqqqqqqqqqqqqqr/8xDE8gAAA0gAAAAAqqqqqqqqqqqqqqqqqv/zEMTyAAADSAAAAACqqqqqqqqqqqqqqqqq//MQxPIAAANIAAAAAKqqqqqqqqqqqqqqqqr/8xDE8gAAA0gAAAAAqqqqqqqqqqqqqqqqqv/zEMTyAAADSAAAAACqqqqqqqqqqqqqqqqq//MQxPIAAANIAAAAAKqqqqqqqqqqqqqqqqr/8xDE8gAAA0gAAAAAqqqqqqqqqqqqqqqqqv/zEMTyAAADSAAAAACqqqqqqqqqqqqqqqqq//MQxPIAAANIAAAAAKqqqqqqqqqqqqqqqqpUQUcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAyMDA1AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/w==";
}
