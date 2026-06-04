using System.Diagnostics;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Drawing.Text;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Win32;

namespace Atlas;

static class Program
{
    internal static readonly string LogFile = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "atlas_crash.log");

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
                MessageBox.Show(e.Exception.Message, "Atlas error");
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

public class AtlasConfig
{

    [JsonPropertyName("monitor")]    public int     MonitorIdx  { get; set; } = 0;
    [JsonPropertyName("resolution")] public string  Res         { get; set; } = "1080p";
    [JsonPropertyName("fps")]        public int     Fps         { get; set; } = 30;
    [JsonPropertyName("audio_name")] public string? AudioDevice { get; set; }
    [JsonPropertyName("duration")]   public string  ClipLen     { get; set; } = "30 sec";
    [JsonPropertyName("hotkey")]     public string  Hotkey      { get; set; } = "f9";
    [JsonPropertyName("output_dir")] public string  OutDir      { get; set; } =
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "clips");
    [JsonPropertyName("topmost")]    public bool    Topmost     { get; set; } = false;

    static string SettingsPath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".atlas.json");

    public static AtlasConfig Load()
    {
        if (!File.Exists(SettingsPath)) return new();
        try {
            return JsonSerializer.Deserialize<AtlasConfig>(File.ReadAllText(SettingsPath)) ?? new();
        }
        catch (JsonException) {

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

        }
    }
}

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
    static readonly string CacheDir  = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Atlas");
    static readonly string CachePath = Path.Combine(CacheDir, "ffmpeg.exe");

    public static string? Locate()
    {

        var next2exe = Path.Combine(AppContext.BaseDirectory, "ffmpeg.exe");
        if (File.Exists(next2exe)) return next2exe;

        if (File.Exists(CachePath)) return CachePath;

        var stream = System.Reflection.Assembly.GetExecutingAssembly()
            .GetManifestResourceStream("Atlas.ffmpeg.exe");
        if (stream != null)
        {
            try
            {
                Directory.CreateDirectory(CacheDir);
                using var fs = File.Create(CachePath);
                stream.CopyTo(fs);
                return CachePath;
            }
            catch { }
        }

        foreach (var dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(';'))
        {
            var p = Path.Combine(dir.Trim(), "ffmpeg.exe");
            if (File.Exists(p)) return p;
        }
        return null;
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
        catch {  }
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

public class HotkeyCapture : Control
{
    bool listening;
    string current;

    static readonly Color bgColor    = Color.FromArgb(0x1E, 0x1E, 0x1E);
    static readonly Color edgeIdle   = Color.FromArgb(0x3C, 0x3C, 0x3C);
    static readonly Color edgeFocus  = Color.FromArgb(0x70, 0x70, 0x70);
    static readonly Color edgeListen = Color.FromArgb(0xA0, 0x60, 0x20);
    static readonly Color textColor  = Color.FromArgb(0xE2, 0xE2, 0xE2);
    static readonly Color hintColor  = Color.FromArgb(0x55, 0x55, 0x55);

    public string Hotkey
    {
        get => current;
        set { current = value; Invalidate(); }
    }

    public event EventHandler? HotkeyChanged;

    public HotkeyCapture(string initial = "")
    {
        current = initial;
        Height  = 26;
        Cursor  = Cursors.Hand;
        Font    = new Font("Segoe UI", 9f);
        SetStyle(ControlStyles.UserPaint | ControlStyles.OptimizedDoubleBuffer |
                 ControlStyles.AllPaintingInWmPaint | ControlStyles.Selectable, true);
        TabStop = true;
    }

    protected override void OnClick(EventArgs e)     { base.OnClick(e); Focus(); listening = true; Invalidate(); }
    protected override void OnGotFocus(EventArgs e)  { Invalidate(); base.OnGotFocus(e); }
    protected override void OnLostFocus(EventArgs e) { listening = false; Invalidate(); base.OnLostFocus(e); }
    protected override bool IsInputKey(Keys k)       => true;

    protected override void OnKeyDown(KeyEventArgs e)
    {
        if (!listening) { base.OnKeyDown(e); return; }
        e.SuppressKeyPress = true;

        if (e.KeyCode is Keys.ControlKey or Keys.LControlKey or Keys.RControlKey
            or Keys.ShiftKey or Keys.LShiftKey or Keys.RShiftKey
            or Keys.Menu or Keys.LMenu or Keys.RMenu
            or Keys.LWin or Keys.RWin)
        { base.OnKeyDown(e); return; }

        if (e.KeyCode == Keys.Escape) { listening = false; Invalidate(); base.OnKeyDown(e); return; }

        var parts = new List<string>();
        if (e.Control) parts.Add("ctrl");
        if (e.Alt)     parts.Add("alt");
        if (e.Shift)   parts.Add("shift");

        string key = e.KeyCode switch {
            >= Keys.F1  and <= Keys.F24 => e.KeyCode.ToString().ToLower(),
            >= Keys.D0  and <= Keys.D9  => ((int)e.KeyCode - (int)Keys.D0).ToString(),
            Keys.Oemtilde    => "`",
            Keys.OemMinus    => "-",
            Keys.Oemplus     => "=",
            Keys.OemOpenBrackets => "[",
            Keys.Oem6        => "]",
            Keys.Oem5        => "\\",
            Keys.OemSemicolon=> ";",
            Keys.OemQuotes   => "'",
            Keys.Oemcomma    => ",",
            Keys.OemPeriod   => ".",
            Keys.OemQuestion => "/",
            Keys.Space       => "space",
            Keys.Insert      => "insert",
            Keys.Delete      => "delete",
            Keys.Home        => "home",
            Keys.End         => "end",
            Keys.Prior       => "pgup",
            Keys.Next        => "pgdn",
            _                => e.KeyCode.ToString().ToLower(),
        };
        parts.Add(key);

        bool isFKey = e.KeyCode >= Keys.F1 && e.KeyCode <= Keys.F24;
        if (parts.Count >= 2 || isFKey)
        {
            current   = string.Join("+", parts);
            listening = false;
            Invalidate();
            HotkeyChanged?.Invoke(this, EventArgs.Empty);
        }

        base.OnKeyDown(e);
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        var g = e.Graphics;
        g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit;
        g.Clear(bgColor);

        var edge = listening ? edgeListen : Focused ? edgeFocus : edgeIdle;
        g.DrawRectangle(new Pen(edge, 1f), 0, 0, Width - 1, Height - 1);

        using var sf = new StringFormat { LineAlignment = StringAlignment.Center };
        if (listening)
            g.DrawString("Press keys…", Font, new SolidBrush(hintColor),
                new RectangleF(8, 0, Width - 16, Height), sf);
        else
            g.DrawString(current, Font, new SolidBrush(textColor),
                new RectangleF(8, 0, Width - 16, Height), sf);
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

        int cx = Width - 13, cy = Height / 2;
        PointF[] tri = open
            ? [new(cx - 3.5f, cy + 1.5f), new(cx + 3.5f, cy + 1.5f), new(cx, cy - 2.5f)]
            : [new(cx - 3.5f, cy - 1.5f), new(cx + 3.5f, cy - 1.5f), new(cx, cy + 2.5f)];
        g.FillPolygon(new SolidBrush(open ? arrOn : arrOff), tri);
    }
}

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

        var workArea = Screen.FromPoint(screenPos).WorkingArea;
        int y = screenPos.Y + totalH > workArea.Bottom
            ? screenPos.Y - totalH - 26
            : screenPos.Y;
        Location = new Point(screenPos.X, y);

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
        if (NeedsSB && e.X >= Width - SB_W - 2) return;

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

    const int CLSCTX_ALL = 23, eRender = 0, eConsole = 1, STGM_READ = 0, SHARED_MODE = 0;
    const int S_OK = 0;
    const uint LOOPBACK_FLAG = 0x00020000, AUTOCONVERT = 0x80000000, SRC_QUALITY = 0x08000000;
    const uint SILENT_PACKET = 2;
    const int TARGET_SR = 44100;

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
        bool started = startedEvent.Wait(3000);

        if (!started) {
            running = false;
            return false;
        }
        return true;
    }

    public void Stop() { running = false; loopThread?.Join(3000); }

    void ThreadMain()
    {
        CoInitializeEx(IntPtr.Zero, 0);

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

                var wantedFmt = new WAVEFORMATEX {
                    wFormatTag      = 3,

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
            if (pv.vt == 31) return Marshal.PtrToStringUni(pv.ptr);

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

        try {
            var drive = new DriveInfo(Path.GetPathRoot(outputDir) ?? "C:\\");
            int clipSecs = videoFrames.Count > 1
                ? (int)(videoFrames[^1].ts - videoFrames[0].ts) + 1
                : 30;
            long estimatedBytes = (long)(clipSecs * (fps >= 30 ? 5_500_000L : 2_800_000L));
            if (drive.AvailableFreeSpace < estimatedBytes * 2)
                System.Diagnostics.Debug.WriteLine($"low disk space warning: {drive.AvailableFreeSpace / 1_000_000}MB free");
        }
        catch (Exception) {  }

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

        double realFps = fps;
        if (videoFrames.Count >= 2) {
            double span = videoFrames[^1].ts - videoFrames[0].ts;
            if (span > 0.5) realFps = Math.Max(1.0, (videoFrames.Count - 1) / span);
        }

        var args = new List<string> {
            "-y", "-f", "image2pipe",
            "-framerate", realFps.ToString("F4", System.Globalization.CultureInfo.InvariantCulture),
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
                catch (IOException) {  }
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
    const int HK = 1;

    const uint CTRL = 0x0002, ALT = 0x0001, SHIFT = 0x0004, WIN = 0x0008, NO_REPEAT = 0x4000;

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
    AtlasConfig    cfg;
    List<DisplayInfo> monitors = [];
    List<string>      audioDevs = [];

    static readonly Dictionary<string, int> DurationMap = new() {
        ["5 sec"]  = 5,
        ["10 sec"] = 10,
        ["15 sec"] = 15,
        ["30 sec"] = 30,
        ["1 min"]  = 60,
    };

    Panel       recDot    = null!;
    bool        dotBlink;
    Label       timeLabel = null!, statusLabel = null!;
    ThinBar     progressBar = null!;
    DropPicker  monPicker = null!, resPicker = null!, fpsPicker = null!,
                audPicker = null!, durPicker = null!;
    HotkeyCapture hotkeyInput = null!;
    StyledTextBox saveDirInput = null!;
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
        cfg = AtlasConfig.Load();
        FormBorderStyle = FormBorderStyle.None;
        BackColor       = Color.FromArgb(0x28, 0x28, 0x28);
        base.Text       = "Atlas";
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

            try { int v = 2; DwmSetWindowAttribute(Handle, 33, ref v, sizeof(int)); } catch { }

            try {
                using var ms = new System.IO.MemoryStream(Convert.FromBase64String(IconB64));
                using var bmp = new Bitmap(ms);
                Icon = Icon.FromHandle(bmp.GetHicon());
            } catch { }

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
                statusLabel.Text = "ffmpeg not found — drop ffmpeg.exe next to Atlas.exe or add it to PATH";
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
        int hkCtrlW = FORM_W - PAD * 2 - LABEL_W - 8;
        AddRowLabel("Hotkey");
        hotkeyInput = new HotkeyCapture(cfg.Hotkey) {
            Location = new Point(PAD + LABEL_W + 8, layoutY - ROW_H + (ROW_H - 26) / 2),
            Size     = new Size(hkCtrlW, 26),
        };
        hotkeyInput.HotkeyChanged += (_, _) => ScheduleRestart();
        root.Controls.Add(hotkeyInput);
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
            Text = "Atlas", ForeColor = FG, Font = F9B,
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
        minBtn.Click        += (_, _) => ShowWindow(Handle, 6);

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
        if (durPicker.SelectedIndex < 0) durPicker.SelectedIndex = 3;

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
        cfg.Hotkey       = hotkeyInput.Hotkey;
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

        if (hotkeyOk)
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
                    else if (part == "space")  vk = (uint)Keys.Space;
                    else if (part == "insert") vk = (uint)Keys.Insert;
                    else if (part == "delete") vk = (uint)Keys.Delete;
                    else if (part == "home")   vk = (uint)Keys.Home;
                    else if (part == "end")    vk = (uint)Keys.End;
                    else if (part == "pgup")   vk = (uint)Keys.Prior;
                    else if (part == "pgdn")   vk = (uint)Keys.Next;
                    else if (part.StartsWith("f") && int.TryParse(part[1..], out int fn) && fn is >= 1 and <= 24)
                        vk = (uint)(Keys.F1 + fn - 1);
                    break;
            }
        }
        if (vk == 0) { statusLabel.Text = $"Couldn't parse hotkey: {hk}"; return; }
        bool ok = RegisterHotKey(Handle, HK, mods | NO_REPEAT, vk);
        if (!ok) statusLabel.Text = $"⚠ {hk} is taken by another app — click Hotkey and press a new combo";
        hotkeyOk = ok;
    }
    bool hotkeyOk;

    void RefreshTimecode()
    {
        if (recCts == null || recCts.IsCancellationRequested) return;
        int cap = DurationMap.TryGetValue(cfg.ClipLen, out int d) ? d : 30;
        double elapsed = Math.Min((DateTime.UtcNow - recStarted).TotalSeconds, cap);
        progressBar.SetProgress(elapsed, cap);
        timeLabel.Text = $"{(int)elapsed}s / {cap}s";

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
        Task.Run(PlaySaveChime);

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

            string path = info.Contains(' ') ? info[..info.IndexOf(' ')] : info;
            string meta = info.Contains(' ') ? info[info.IndexOf(' ')..].Trim() : "";
            lastClipPath = path;
            previewBtn.Enabled = true;

            int drops = audioRec?.LatePackets ?? 0;
            string dropNote = drops > 0 ? $"  ·  {drops} late audio pkts" : "";
            statusLabel.Text = $"Saved {Path.GetFileName(path)} {meta}{dropNote}";
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

        var t = new Thread(() =>
        {
            try
            {
                var mp3 = Convert.FromBase64String(SoundB64);
                var tmp = Path.Combine(Path.GetTempPath(), "atlas_chime.mp3");
                File.WriteAllBytes(tmp, mp3);
                string alias = "ac" + Environment.TickCount64.ToString("X");
                int r1 = mciSendString($"open \"{tmp}\" type mpegvideo alias {alias}", null, 0, IntPtr.Zero);
                int r2 = mciSendString($"play {alias} wait", null, 0, IntPtr.Zero);
                int r3 = mciSendString($"close {alias}", null, 0, IntPtr.Zero);
                if (r1 != 0 || r2 != 0)
                    File.AppendAllText(Program.LogFile, $"[sound] open={r1} play={r2} close={r3} file={tmp}\n");
                try { File.Delete(tmp); } catch { }
            }
            catch (Exception ex) { File.AppendAllText(Program.LogFile, $"[sound ex] {ex.Message}\n"); }
        });
        t.SetApartmentState(ApartmentState.STA);
        t.IsBackground = true;
        t.Start();
    }

    const string IconB64 = "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";

    const string SoundB64 = "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My4xMDCqqqqqTEFNRTMuMTAwqqqqqkxBTUUzLjEwMKqqqqpMQU1FMy4xMDCqqqqqTEFNRTMuMTAwqqqqqkxBTUUzLjEwMKqqqqpMQU1FMy4xMDCqqqqqTEFNRTMuMTAwqqqqqkxBTUUzLjEwMKqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqr/8xDE8AAAA0gBwAAAqqqqqqqqqqqqqqqqqv/zEMTyAAADSAAAAACqqqqqqqqqqqqqqqqq//MQxPIAAANIAAAAAKqqqqqqqqqqqqqqqqr/8xDE8gAAA0gAAAAAqqqqqqqqqqqqqqqqqv/zEMTyAAADSAAAAACqqqqqqqqqqqqqqqqq//MQxPIAAANIAAAAAKqqqqqqqqqqqqqqqqr/8xDE8gAAA0gAAAAAqqqqqqqqqqqqqqqqqv/zEMTyAAADSAAAAACqqqqqqqqqqqqqqqqq//MQxPIAAANIAAAAAKqqqqqqqqqqqqqqqqr/8xDE8gAAA0gAAAAAqqqqqqqqqqqqqqqqqv/zEMTyAAADSAAAAACqqqqqqqqqqqqqqqqq//MQxPIAAANIAAAAAKqqqqqqqqqqqqqqqqr/8xDE8gAAA0gAAAAAqqqqqqqqqqqqqqqqqv/zEMTyAAADSAAAAACqqqqqqqqqqqqqqqqq//MQxPIAAANIAAAAAKqqqqqqqqqqqqqqqqr/8xDE8gAAA0gAAAAAqqqqqqqqqqqqqqqqqv/zEMTyAAADSAAAAACqqqqqqqqqqqqqqqqq//MQxPIAAANIAAAAAKqqqqqqqqqqqqqqqqpUQUcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAyMDA1AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/w==";
}
