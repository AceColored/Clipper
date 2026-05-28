import os, sys, time, wave, threading, subprocess, collections, json, importlib.util
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog

try:
    import ctypes
    ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
except Exception:
    pass

def _bootstrap():
    needed = [
        ('keyboard',      'keyboard'),
        ('mss',           'mss'),
        ('cv2',           'opencv-python'),
        ('sounddevice',   'sounddevice'),
        ('numpy',         'numpy'),
        ('imageio_ffmpeg','imageio-ffmpeg'),
    ]
    missing = [pkg for mod, pkg in needed if importlib.util.find_spec(mod) is None]
    if not missing:
        return
    splash = tk.Tk()
    splash.overrideredirect(True)
    splash.configure(bg='#0d0d0d')
    w, h = 320, 90
    splash.geometry(f'{w}x{h}+{splash.winfo_screenwidth()//2-w//2}+{splash.winfo_screenheight()//2-h//2}')
    tk.Label(splash, text='Setting up Clipper…', bg='#0d0d0d', fg='#eeeeee',
             font=('Segoe UI', 11, 'bold')).pack(pady=(20, 4))
    tk.Label(splash, text='Installing packages, one moment…', bg='#0d0d0d', fg='#565659',
             font=('Segoe UI', 9)).pack()
    splash.update()
    subprocess.check_call(
        [sys.executable, '-m', 'pip', 'install', '--quiet'] + missing,
        creationflags=subprocess.CREATE_NO_WINDOW)
    splash.destroy()

_bootstrap()

import numpy as np
import keyboard, mss, cv2, sounddevice as sd
import imageio_ffmpeg

SAMPLE_RATE  = 44100
CHANNELS     = 2
JPEG_QUALITY = 78
MAX_BUF_SECS = 65
CONFIG_PATH  = Path.home() / '.clipper.json'
RES_H        = {'360p': 360, '720p': 720, '1080p': 1080}
DURATIONS    = {'5 sec': 5, '10 sec': 10, '15 sec': 15, '30 sec': 30, '1 min': 60}
DEFAULTS     = {'monitor': 0, 'resolution': '1080p', 'fps': 30, 'audio_name': None,
                'duration': '30 sec', 'hotkey': 'ctrl+c',
                'output_dir': str(Path.home() / 'clips')}

def _load_cfg():
    cfg = dict(DEFAULTS)
    try: cfg.update(json.loads(CONFIG_PATH.read_text()))
    except Exception: pass
    if isinstance(cfg.get('duration'), int):
        cfg['duration'] = {5:'5 sec',10:'10 sec',15:'15 sec',30:'30 sec',60:'1 min'}.get(cfg['duration'],'30 sec')
    return cfg

def _save_cfg(cfg):
    try: CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except Exception: pass

_vbuf    = collections.deque()
_abuf    = collections.deque()
_lock    = threading.Lock()
_stop_ev = threading.Event()
_saving  = False

def _get_monitors():
    with mss.mss() as sct:
        return sct.monitors[1:]

def _edid_name(edid: bytes) -> str:
    for i in range(4):
        o = 54 + i * 18
        if len(edid) < o + 18:
            break
        if edid[o:o+3] == b'\x00\x00\x00' and edid[o+3] == 0xFC:
            return edid[o+5:o+18].decode('cp437', errors='ignore').rstrip('\n').rstrip()
    return ''

def _get_monitor_names():
    try:
        import ctypes, winreg, re
        from ctypes import wintypes, Structure, c_wchar

        class DD(Structure):
            _fields_ = [('cb', wintypes.DWORD), ('DeviceName', c_wchar*32),
                        ('DeviceString', c_wchar*128), ('StateFlags', wintypes.DWORD),
                        ('DeviceID', c_wchar*128), ('DeviceKey', c_wchar*128)]

        names = []
        i = 0
        while True:
            adp = DD(); adp.cb = ctypes.sizeof(DD)
            if not ctypes.windll.user32.EnumDisplayDevicesW(None, i, ctypes.byref(adp), 0):
                break
            if adp.StateFlags & 1:
                mon = DD(); mon.cb = ctypes.sizeof(DD)
                ctypes.windll.user32.EnumDisplayDevicesW(adp.DeviceName, 0, ctypes.byref(mon), 1)
                name = ''
                m = re.search(r'DISPLAY#([^#]+)#', mon.DeviceID or '')
                if m:
                    hw_id = m.group(1)
                    try:
                        base = rf'SYSTEM\CurrentControlSet\Enum\DISPLAY\{hw_id}'
                        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as hk:
                            k = 0
                            while not name and k < 20:
                                try:
                                    sub = winreg.EnumKey(hk, k)
                                    with winreg.OpenKey(hk, rf'{sub}\Device Parameters') as pk:
                                        edid, _ = winreg.QueryValueEx(pk, 'EDID')
                                        name = _edid_name(bytes(edid))
                                except Exception:
                                    pass
                                k += 1
                    except Exception:
                        pass
                names.append(name or f'Display {len(names)+1}')
            i += 1
        return names
    except Exception:
        return []

def _get_inputs():
    return [(i, d['name']) for i, d in enumerate(sd.query_devices()) if d['max_input_channels'] >= 1]

def _find_loopback(devices):
    apis = sd.query_hostapis()
    wasapi = next((i for i, a in enumerate(apis) if 'wasapi' in a['name'].lower()), None)
    if wasapi is None: return None
    devs = list(sd.query_devices())
    for idx, _ in devices:
        d = devs[idx]
        if d['hostapi'] == wasapi and any(k in d['name'].lower() for k in ('loopback','stereo mix','what u hear','wave out')):
            return idx
    for idx, _ in devices:
        if devs[idx]['hostapi'] == wasapi: return idx
    return None

def _scaled_size(monitor, res_label):
    nw, nh = monitor['width'], monitor['height']
    th = RES_H.get(res_label, nh)
    if nh <= th: return nw, nh
    scale = th / nh
    return int(nw * scale) & ~1, int(nh * scale) & ~1

def _audio_cb(indata, frames, t, status):
    if not _stop_ev.is_set():
        now = time.time()
        with _lock:
            _abuf.append((now, indata.copy()))
            cutoff = now - MAX_BUF_SECS
            while _abuf and _abuf[0][0] < cutoff:
                _abuf.popleft()

def _capture_loop(monitor, w, h, fps, stop_ev):
    interval = 1.0 / fps
    nxt = time.perf_counter()
    with mss.mss() as sct:
        while not stop_ev.is_set():
            now = time.time()
            frame = np.array(sct.grab(monitor))
            bgr = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
            if (bgr.shape[1], bgr.shape[0]) != (w, h):
                bgr = cv2.resize(bgr, (w, h), interpolation=cv2.INTER_AREA)
            ok, enc = cv2.imencode('.jpg', bgr, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if ok:
                with _lock:
                    _vbuf.append((now, enc.tobytes()))
                    cutoff = now - MAX_BUF_SECS
                    while _vbuf and _vbuf[0][0] < cutoff:
                        _vbuf.popleft()
            nxt += interval
            wait = nxt - time.perf_counter()
            if wait > 0:
                time.sleep(wait)

def _save(dur_secs, fps, w, h, out_dir, ffmpeg, on_done):
    global _saving
    now = time.time()
    cutoff = now - dur_secs
    with _lock:
        frames  = [(t, d) for t, d in _vbuf if t >= cutoff]
        achunks = [(t, d) for t, d in _abuf if t >= cutoff]

    if not frames:
        _saving = False
        on_done(False, 'Buffer empty — wait a moment after launch.')
        return

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp      = datetime.now().strftime('%Y%m%d_%H%M%S')
    video_path = out / f'raw_{stamp}.mp4'
    audio_path = out / f'audio_{stamp}.wav'
    final_path = out / f'clip_{stamp}.mp4'

    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    writer = cv2.VideoWriter(str(video_path), fourcc, fps, (w, h))
    for _, jpeg in frames:
        img = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is not None:
            writer.write(img)
    writer.release()

    has_audio = bool(achunks)
    if has_audio:
        arr = np.concatenate([c for _, c in achunks], axis=0)
        pcm = (np.clip(arr, -1.0, 1.0) * 32767).astype(np.int16)
        with wave.open(str(audio_path), 'wb') as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm.tobytes())

    cmd = [ffmpeg, '-y', '-i', str(video_path)]
    if has_audio:
        cmd += ['-i', str(audio_path)]
    cmd += ['-c:v', 'libx264', '-preset', 'fast', '-crf', '23']
    if has_audio:
        cmd += ['-c:a', 'aac', '-b:a', '192k', '-shortest']
    cmd.append(str(final_path))

    result = subprocess.run(cmd, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    video_path.unlink(missing_ok=True)
    if has_audio and audio_path.exists():
        audio_path.unlink()

    _saving = False
    if result.returncode == 0:
        on_done(True, str(final_path))
    else:
        on_done(False, result.stderr.decode(errors='replace'))

BG      = '#0d0d0d'
CARD    = '#131315'
CARD_B  = '#1b1b1e'
BORDER  = '#242427'
BORDER2 = '#2e2e32'
ACCENT  = '#e74c3c'
TEXT    = '#eeeeee'
TEXT2   = '#565659'
FONT    = 'Segoe UI'


class _Bar(tk.Canvas):
    def __init__(self, parent, h=6, **kw):
        super().__init__(parent, height=h, bg=CARD, highlightthickness=0, **kw)
        self._v = 0; self._m = 1
        self.bind('<Configure>', lambda e: self._draw())

    def set(self, val, maximum=1):
        self._v = min(val, maximum); self._m = max(maximum, 1); self._draw()

    def _draw(self):
        self.delete('all')
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4: return
        self._rr(0, 0, w, h, CARD_B)
        if self._m and self._v:
            fw = int(w * self._v / self._m)
            if fw >= h: self._rr(0, 0, fw, h, ACCENT)

    def _rr(self, x1, y1, x2, y2, c):
        h = y2 - y1
        self.create_arc(x1, y1, x1+h, y2, start=90, extent=180, fill=c, outline=c)
        mid1, mid2 = x1+h//2, x2-h//2
        if mid2 > mid1:
            self.create_rectangle(mid1, y1, mid2, y2, fill=c, outline=c)
        self.create_arc(x2-h, y1, x2, y2, start=270, extent=180, fill=c, outline=c)


def _card(parent, padx=14, pady=12):
    outer = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
    outer.pack(fill='x', padx=14, pady=4)
    inner = tk.Frame(outer, bg=CARD)
    inner.pack(fill='both', expand=True, padx=padx, pady=pady)
    return inner

def _label(parent, text):
    return tk.Label(parent, text=text, bg=CARD, fg=TEXT2, font=(FONT, 9))

def _btn(parent, text, cmd, state='normal'):
    return tk.Button(parent, text=text, command=cmd, state=state,
                     bg=CARD_B, fg=TEXT, activebackground=BORDER2,
                     activeforeground=TEXT, disabledforeground=TEXT2,
                     relief='flat', bd=0, padx=12, pady=6, font=(FONT, 9),
                     highlightthickness=1, highlightbackground=BORDER2,
                     cursor='hand2')

def _radio_row(parent, var, options):
    f = tk.Frame(parent, bg=CARD)
    for val in options:
        tk.Radiobutton(f, text=val, variable=var, value=val,
                       bg=CARD, fg=TEXT, selectcolor=CARD,
                       activebackground=CARD, activeforeground=TEXT,
                       font=(FONT, 9), bd=0, highlightthickness=0,
                       cursor='hand2').pack(side='left', padx=(0, 12))
    return f

def _entry(parent, var, width=20):
    return tk.Entry(parent, textvariable=var, width=width,
                    bg=CARD_B, fg=TEXT, font=(FONT, 9),
                    relief='flat', bd=0, insertbackground=TEXT,
                    highlightthickness=1, highlightbackground=BORDER2,
                    highlightcolor=ACCENT)


class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.overrideredirect(True)
        self.resizable(False, False)
        self.configure(bg=BG)
        self._dx = self._dy = 0
        self._ffmpeg    = imageio_ffmpeg.get_ffmpeg_exe()
        self._monitors  = _get_monitors()
        self._inputs    = _get_inputs()
        self._cfg       = _load_cfg()
        self._stream    = None
        self._cap_start = None
        self._last_clip = None
        self._tick_job  = None
        self._cur_fps   = 30
        self._cur_w     = 1920
        self._cur_h     = 1080
        self._pb        = True
        self._style_ttk()
        self._build()
        self.protocol('WM_DELETE_WINDOW', self._quit)
        self.after(20,  self._fix_win)
        self.after(150, self._start)

    def _style_ttk(self):
        s = ttk.Style(self)
        s.theme_use('clam')
        s.configure('TCombobox', fieldbackground=CARD_B, background=CARD_B,
                    foreground=TEXT, arrowcolor=TEXT2, bordercolor=BORDER2,
                    darkcolor=CARD_B, lightcolor=CARD_B,
                    selectbackground=BORDER2, selectforeground=TEXT,
                    padding=(6, 5))
        s.map('TCombobox',
              fieldbackground=[('readonly', CARD_B), ('focus', CARD_B)],
              selectbackground=[('readonly', CARD_B)],
              bordercolor=[('focus', ACCENT)])
        self.option_add('*TCombobox*Listbox.background', CARD_B)
        self.option_add('*TCombobox*Listbox.foreground', TEXT)
        self.option_add('*TCombobox*Listbox.selectBackground', '#2a2a2e')
        self.option_add('*TCombobox*Listbox.selectForeground', TEXT)
        self.option_add('*TCombobox*Listbox.font', (FONT, 9))
        self.option_add('*TCombobox*Listbox.relief', 'flat')

    def _fix_win(self):
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetAncestor(self.winfo_id(), 2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)
            GWL_EXSTYLE = -20
            cur = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, (cur | 0x00040000) & ~0x00000080)
            self.withdraw()
            self.deiconify()
        except Exception:
            pass

    def _build_titlebar(self):
        tb = tk.Frame(self, bg=BG, height=32)
        tb.pack(fill='x')
        tb.pack_propagate(False)
        tk.Label(tb, text='● Clipper', bg=BG, fg=TEXT2,
                 font=(FONT, 9)).pack(side='left', padx=12)
        for sym, cmd, hbg, hfg in [
            ('✕', self._quit,     '#c0392b', '#ffffff'),
            ('⎯', self._minimize, '#1e1e21', TEXT),
        ]:
            lbl = tk.Label(tb, text=sym, bg=BG, fg=TEXT2,
                           font=(FONT, 10), padx=14, cursor='hand2')
            lbl.pack(side='right', fill='y')
            lbl.bind('<Button-1>', lambda e, c=cmd: c())
            lbl.bind('<Enter>',   lambda e, l=lbl, b=hbg, f=hfg: l.config(bg=b, fg=f))
            lbl.bind('<Leave>',   lambda e, l=lbl: l.config(bg=BG, fg=TEXT2))
        tk.Frame(self, bg=BORDER, height=1).pack(fill='x')
        tb.bind('<ButtonPress-1>',
                lambda e: (setattr(self, '_dx', e.x_root - self.winfo_x()),
                           setattr(self, '_dy', e.y_root - self.winfo_y())))
        tb.bind('<B1-Motion>',
                lambda e: self.geometry(f'+{e.x_root-self._dx}+{e.y_root-self._dy}'))

    def _minimize(self):
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetAncestor(self.winfo_id(), 2)
            ctypes.windll.user32.ShowWindow(hwnd, 6)
        except Exception:
            self.iconify()

    def _build(self):
        self._build_titlebar()
        tk.Frame(self, bg=BG, height=4).pack()

        sc = _card(self, pady=10)
        row = tk.Frame(sc, bg=CARD)
        row.pack(fill='x')
        self._dot_cv = tk.Canvas(row, width=10, height=10, bg=CARD, highlightthickness=0)
        self._dot_cv.pack(side='left', padx=(0, 7))
        self._dot_id = self._dot_cv.create_oval(1, 1, 9, 9, fill=ACCENT, outline='')
        tk.Label(row, text='REC', bg=CARD, fg=TEXT, font=(FONT, 9, 'bold')).pack(side='left')
        self._buf_var = tk.StringVar(value='')
        tk.Label(row, textvariable=self._buf_var, bg=CARD, fg=TEXT2, font=(FONT, 9)).pack(side='right')
        self._bar = _Bar(sc, h=5)
        self._bar.pack(fill='x', pady=(8, 0))

        stc = _card(self)
        G = {'sticky': 'w', 'padx': (0, 10), 'pady': 4}

        _label(stc, 'Monitor').grid(row=0, column=0, **G)
        friendly  = _get_monitor_names()
        mon_names = [
            f"{friendly[i]}  ({m['width']}×{m['height']})"
            if i < len(friendly) and friendly[i]
            else f"Monitor {i+1}  ({m['width']}×{m['height']})"
            for i, m in enumerate(self._monitors)
        ]
        self._mon_var = tk.StringVar()
        self._mon_cb  = ttk.Combobox(stc, textvariable=self._mon_var,
                                     values=mon_names, width=30, state='readonly')
        self._mon_cb.grid(row=0, column=1, columnspan=2, sticky='ew', pady=4)
        self._mon_cb.current(min(self._cfg['monitor'], len(mon_names)-1))

        _label(stc, 'Resolution').grid(row=1, column=0, **G)
        self._res_var = tk.StringVar(value=self._cfg['resolution'])
        _radio_row(stc, self._res_var, ('360p', '720p', '1080p')).grid(
            row=1, column=1, columnspan=2, sticky='w', pady=4)

        _label(stc, 'FPS').grid(row=2, column=0, **G)
        self._fps_var = tk.StringVar(value=str(self._cfg['fps']))
        _radio_row(stc, self._fps_var, ('15', '30', '60')).grid(
            row=2, column=1, columnspan=2, sticky='w', pady=4)

        _label(stc, 'Audio').grid(row=3, column=0, **G)
        AUTO = 'Auto — All PC Sounds  (recommended)'
        self._auto_label = AUTO
        dev_names = [AUTO] + [n for _, n in self._inputs]
        self._aud_var = tk.StringVar()
        self._aud_cb  = ttk.Combobox(stc, textvariable=self._aud_var,
                                     values=dev_names, width=30, state='readonly')
        self._aud_cb.grid(row=3, column=1, columnspan=2, sticky='ew', pady=4)
        saved = self._cfg.get('audio_name')
        self._aud_cb.current(dev_names.index(saved) if saved and saved in dev_names else 0)

        _label(stc, 'Clip Length').grid(row=4, column=0, **G)
        self._dur_var = tk.StringVar(value=self._cfg['duration'])
        _radio_row(stc, self._dur_var, list(DURATIONS.keys())).grid(
            row=4, column=1, columnspan=2, sticky='w', pady=4)

        _label(stc, 'Hotkey').grid(row=5, column=0, **G)
        self._hk_var = tk.StringVar(value=self._cfg['hotkey'])
        _entry(stc, self._hk_var, width=18).grid(row=5, column=1, sticky='w', pady=4)

        _label(stc, 'Save To').grid(row=6, column=0, **G)
        self._dir_var = tk.StringVar(value=self._cfg['output_dir'])
        _entry(stc, self._dir_var, width=22).grid(row=6, column=1, sticky='ew', pady=4)
        _btn(stc, 'Browse…', self._browse).grid(row=6, column=2, padx=(6, 0), pady=4)

        tk.Frame(stc, bg=BORDER, height=1).grid(
            row=7, column=0, columnspan=3, sticky='ew', pady=(8, 6))

        bot = tk.Frame(stc, bg=CARD)
        bot.grid(row=8, column=0, columnspan=3, sticky='ew')
        self._topmost_var = tk.BooleanVar(value=self._cfg.get('topmost', False))
        tk.Checkbutton(bot, text='Always on top', variable=self._topmost_var,
                       bg=CARD, fg=TEXT, selectcolor=CARD_B,
                       activebackground=CARD, activeforeground=TEXT,
                       font=(FONT, 9), bd=0, highlightthickness=0,
                       cursor='hand2', command=self._toggle_topmost).pack(side='left')
        _btn(bot, 'Apply Settings', self._apply).pack(side='right')

        stc.columnconfigure(1, weight=1)

        ac = _card(self, pady=10)
        brow = tk.Frame(ac, bg=CARD)
        brow.pack(fill='x')
        self._prev_btn = _btn(brow, '▶  Preview Last Clip', self._preview, state='disabled')
        self._prev_btn.pack(side='left', padx=(0, 8))
        _btn(brow, '📂  Open Clips Folder', self._open_folder).pack(side='left')
        self._sv = tk.StringVar(value='Starting…')
        tk.Label(ac, textvariable=self._sv, bg=CARD, fg=TEXT2,
                 font=(FONT, 8)).pack(anchor='w', pady=(7, 0))

        tk.Frame(self, bg=BG, height=6).pack()

        if self._topmost_var.get():
            self.wm_attributes('-topmost', True)

        self._pulse()

    def _pulse(self):
        self._pb = not self._pb
        self._dot_cv.itemconfig(self._dot_id, fill=ACCENT if self._pb else '#5c1a14')
        self.after(750, self._pulse)

    def _toggle_topmost(self):
        self.wm_attributes('-topmost', self._topmost_var.get())

    def _start(self):
        global _stop_ev, _vbuf, _abuf
        _stop_ev = threading.Event()
        with _lock:
            _vbuf.clear(); _abuf.clear()

        mon_idx = self._mon_cb.current()
        monitor = self._monitors[max(mon_idx, 0)]
        fps     = int(self._fps_var.get())
        w, h    = _scaled_size(monitor, self._res_var.get())
        self._cur_fps = fps; self._cur_w = w; self._cur_h = h

        sel = self._aud_cb.current()
        if sel <= 0 or not self._inputs:
            dev_idx = _find_loopback(self._inputs)
            if dev_idx is None and self._inputs: dev_idx = self._inputs[0][0]
        else:
            dev_idx = self._inputs[sel - 1][0]
        ch = CHANNELS
        if dev_idx is not None:
            try: ch = min(CHANNELS, int(sd.query_devices(dev_idx)['max_input_channels'])) or 1
            except Exception: pass
        try:
            kw = dict(samplerate=SAMPLE_RATE, channels=ch, dtype='float32', callback=_audio_cb)
            if dev_idx is not None: kw['device'] = dev_idx
            self._stream = sd.InputStream(**kw); self._stream.start()
        except Exception:
            self._stream = None

        hk = self._hk_var.get().strip()
        try: keyboard.add_hotkey(hk, self._on_hotkey, suppress=True)
        except Exception: pass

        threading.Thread(target=_capture_loop,
                         args=(monitor, w, h, fps, _stop_ev), daemon=True).start()
        self._cap_start = time.time()
        self._sv.set(f'Press  {hk}  to clip the last {self._dur_var.get()}.')
        self._tick()

    def _stop(self):
        _stop_ev.set()
        try: keyboard.remove_all_hotkeys()
        except Exception: pass
        if self._stream:
            try: self._stream.stop(); self._stream.close()
            except Exception: pass
            self._stream = None
        if self._tick_job:
            self.after_cancel(self._tick_job); self._tick_job = None

    def _apply(self):
        sel_mon = self._mon_cb.current()
        sel_aud = self._aud_cb.current()
        self._cfg.update({
            'monitor':    max(sel_mon, 0),
            'resolution': self._res_var.get(),
            'fps':        int(self._fps_var.get()),
            'audio_name': None if sel_aud <= 0 else (self._inputs[sel_aud-1][1] if self._inputs else None),
            'duration':   self._dur_var.get(),
            'hotkey':     self._hk_var.get().strip(),
            'output_dir': self._dir_var.get(),
            'topmost':    self._topmost_var.get(),
        })
        _save_cfg(self._cfg)
        self._stop()
        self._bar.set(0, 1)
        self._sv.set('Restarting…')
        self.after(400, self._start)

    def _tick(self):
        dur     = DURATIONS[self._dur_var.get()]
        elapsed = min(time.time() - self._cap_start, dur)
        self._bar.set(elapsed, dur)
        self._buf_var.set(f'  {int(elapsed)}s / {dur}s')
        self._tick_job = self.after(500, self._tick)

    def _on_hotkey(self): self.after(0, self._clip)

    def _clip(self):
        global _saving
        if _saving: return
        _saving = True
        self._sv.set('Saving clip…')
        dur = DURATIONS[self._dur_var.get()]
        threading.Thread(target=_save, args=(dur, self._cur_fps, self._cur_w, self._cur_h,
            self._cfg['output_dir'], self._ffmpeg, self._on_saved), daemon=False).start()

    def _on_saved(self, ok, info): self.after(0, self._after_save, ok, info)

    def _after_save(self, ok, info):
        if ok:
            self._last_clip = info
            self._prev_btn.config(state='normal')
            self._sv.set(f'Saved  →  {Path(info).name}')
        else:
            self._sv.set('Save failed — see console.')
            print('[error]', info)

    def _preview(self):
        if self._last_clip and Path(self._last_clip).exists():
            os.startfile(self._last_clip)

    def _open_folder(self):
        Path(self._cfg['output_dir']).mkdir(parents=True, exist_ok=True)
        subprocess.Popen(['explorer', self._cfg['output_dir']])

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self._dir_var.get())
        if d: self._dir_var.set(d)

    def _quit(self):
        self._stop(); self.destroy()


if __name__ == '__main__':
    App().mainloop()
