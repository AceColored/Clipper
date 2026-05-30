# deps auto-install on first run; config at ~/.clipper.json
# tested on Win10/11, python 3.10+

import sys, os as _os
if _os.path.basename(sys.executable).lower() == 'python.exe':
    _pw = _os.path.join(_os.path.dirname(sys.executable), 'pythonw.exe')
    if _os.path.exists(_pw):
        import subprocess as _sp
        _sp.Popen([_pw] + sys.argv)
        sys.exit()

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
    deps = [('keyboard','keyboard'), ('mss','mss'), ('cv2','opencv-python'),
            ('sounddevice','sounddevice'), ('numpy','numpy'), ('imageio_ffmpeg','imageio-ffmpeg')]
    missing = [pkg for mod, pkg in deps if importlib.util.find_spec(mod) is None]
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
JPEG_QUALITY = 78   # 85+ roughly doubles buffer size with almost no visible gain
KEEP_SECS = 65  # a bit over the max 60s clip so the ring buffer is never short at save time
CONFIG_PATH  = Path.home() / '.clipper.json'
RES_H        = {'360p': 360, '720p': 720, '1080p': 1080}
DURATIONS    = {'5 sec': 5, '10 sec': 10, '15 sec': 15, '30 sec': 30, '1 min': 60}
DEFAULTS     = {'monitor': 0, 'resolution': '1080p', 'fps': 30, 'audio_name': None,
                'duration': '30 sec', 'hotkey': 'ctrl+c',
                'output_dir': str(Path.home() / 'clips')}

def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except (OSError, json.JSONDecodeError):
        pass
    # old builds stored duration as an int; convert it
    if isinstance(cfg.get('duration'), int):
        cfg['duration'] = {5:'5 sec',10:'10 sec',15:'15 sec',30:'30 sec',60:'1 min'}.get(cfg['duration'],'30 sec')
    return cfg

def save_config(cfg):
    try: CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except OSError: pass

vid_buf  = collections.deque()
aud_buf  = collections.deque()
buf_lock = threading.Lock()
cap_stop = threading.Event()
is_saving = False

def get_monitors():
    with mss.mss() as sct:
        return sct.monitors[1:]

def parse_edid_name(edid: bytes) -> str:
    # descriptor block 0xFC = monitor name; others are range limits, serial, etc.
    # some monitors put the name in block 0 instead of 1 so we check all four
    for i in range(4):
        o = 54 + i * 18
        if len(edid) < o + 18:
            break
        if edid[o:o+3] == b'\x00\x00\x00' and edid[o+3] == 0xFC:
            return edid[o+5:o+18].decode('cp437', errors='ignore').rstrip('\n').rstrip()
    return ''

def get_monitor_names():
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
                                        name = parse_edid_name(bytes(edid))
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

import ctypes.wintypes as _wt

class _WasapiLoopback:
    # Had to roll this manually — sounddevice can't do loopback on output devices,
    # and pyaudiowpatch pulled in too many deps. Raw COM through ctypes is ugly but works.

    _CLSCTX_ALL        = 23
    _SHAREMODE_SHARED  = 0
    _FL_LOOPBACK       = 0x00020000
    _FL_AUTOCONVERT    = 0x80000000
    _FL_SRC_QUALITY    = 0x08000000
    _ERENDER           = 0
    _ECONSOLE          = 1
    _BUFFLAGS_SILENT   = 2
    _S_OK              = 0

    class _GUID(ctypes.Structure):
        _fields_ = [('d1', _wt.DWORD), ('d2', _wt.WORD), ('d3', _wt.WORD),
                    ('d4', ctypes.c_uint8 * 8)]

    class _WFEX(ctypes.Structure):
        _fields_ = [('wFormatTag', _wt.WORD), ('nChannels', _wt.WORD),
                    ('nSamplesPerSec', _wt.DWORD), ('nAvgBytesPerSec', _wt.DWORD),
                    ('nBlockAlign', _wt.WORD), ('wBitsPerSample', _wt.WORD),
                    ('cbSize', _wt.WORD)]

    @staticmethod
    def _guid(s):
        p = s.strip('{}').split('-')
        b = bytes.fromhex(p[3] + p[4])
        G = _WasapiLoopback._GUID
        return G(int(p[0],16), int(p[1],16), int(p[2],16), (G._fields_[3][1])(*b))

    _CLSID = None
    _IID_E = None
    _IID_C = None
    _IID_A = None

    @classmethod
    def _init_guids(cls):
        if cls._CLSID is None:
            cls._CLSID = cls._guid('{BCDE0395-E52F-467C-8E3D-C4579291692E}')
            cls._IID_E = cls._guid('{A95664D2-9614-4F35-A746-DE8DB63617E6}')
            cls._IID_C = cls._guid('{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}')
            cls._IID_A = cls._guid('{C8ADBD64-E71E-48A0-A4DE-185C395CD317}')

    @staticmethod
    def _vcall(iface, idx, res, *atv):
        types, vals = atv[0::2], atv[1::2]
        vt = ctypes.cast(ctypes.cast(iface, ctypes.POINTER(ctypes.c_void_p))[0],
                         ctypes.POINTER(ctypes.c_void_p))
        return ctypes.WINFUNCTYPE(res, ctypes.c_void_p, *types)(vt[idx])(iface, *vals)

    @staticmethod
    def _release(p):
        if p:
            _WasapiLoopback._vcall(p, 2, _wt.ULONG)

    def __init__(self, callback, samplerate=44100, channels=2):
        self._cb   = callback
        self._sr   = samplerate
        self._ch   = channels
        self._stop = threading.Event()
        self._thr  = None

    def start(self):
        self._stop.clear()
        self._thr = threading.Thread(target=self._run, daemon=True)
        self._thr.start()

    def stop(self):
        self._stop.set()
        if self._thr:
            self._thr.join(timeout=3)

    def close(self): self.stop()

    def _run(self):
        ctypes.windll.ole32.CoInitialize(None)
        try:    self._loop()
        finally: ctypes.windll.ole32.CoUninitialize()

    def _loop(self):
        self._init_guids()
        vc = self._vcall
        G  = ctypes.POINTER(self._GUID)
        VP = ctypes.POINTER(ctypes.c_void_p)

        enum = ctypes.c_void_p()
        if ctypes.windll.ole32.CoCreateInstance(
                ctypes.byref(self._CLSID), None, self._CLSCTX_ALL,
                ctypes.byref(self._IID_E), ctypes.byref(enum)) != self._S_OK: return
        try:
            dev = ctypes.c_void_p()
            if vc(enum, 4, _wt.HRESULT,
                  _wt.DWORD, self._ERENDER, _wt.DWORD, self._ECONSOLE,
                  VP, ctypes.byref(dev)) != self._S_OK or not dev: return
            try:
                cli = ctypes.c_void_p()
                if vc(dev, 3, _wt.HRESULT,
                      G, ctypes.byref(self._IID_C), _wt.DWORD, self._CLSCTX_ALL,
                      ctypes.c_void_p, None, VP, ctypes.byref(cli)) != self._S_OK or not cli: return
                try:
                    # float32 stereo at 44100; AUTOCONVERT + SRC_QUALITY handles mismatches
                    W = self._WFEX
                    fmt = W(3, self._ch, self._sr,
                            self._sr * self._ch * 4, self._ch * 4, 32, 0)
                    flags = self._FL_LOOPBACK | self._FL_AUTOCONVERT | self._FL_SRC_QUALITY
                    if vc(cli, 3, _wt.HRESULT,
                          _wt.DWORD, self._SHAREMODE_SHARED, _wt.DWORD, flags,
                          ctypes.c_int64, 10_000_000, ctypes.c_int64, 0,
                          ctypes.POINTER(W), ctypes.byref(fmt),
                          ctypes.c_void_p, None) != self._S_OK: return
                    cap = ctypes.c_void_p()
                    if vc(cli, 14, _wt.HRESULT,
                          G, ctypes.byref(self._IID_A),
                          VP, ctypes.byref(cap)) != self._S_OK or not cap: return
                    try:
                        vc(cli, 10, _wt.HRESULT)  # IAudioClient::Start
                        frame_bytes = self._ch * 4
                        while not self._stop.is_set():
                            avail = _wt.UINT(0)
                            vc(cap, 5, _wt.HRESULT, ctypes.POINTER(_wt.UINT), ctypes.byref(avail))
                            if avail.value == 0:
                                time.sleep(0.005); continue
                            pdata  = ctypes.c_void_p()
                            nfr    = _wt.UINT(0)
                            bflags = _wt.DWORD(0)
                            hr = vc(cap, 3, _wt.HRESULT,
                                    VP, ctypes.byref(pdata),
                                    ctypes.POINTER(_wt.UINT), ctypes.byref(nfr),
                                    ctypes.POINTER(_wt.DWORD), ctypes.byref(bflags),
                                    ctypes.c_void_p, None, ctypes.c_void_p, None)
                            n = nfr.value
                            if hr == self._S_OK and n > 0:
                                if (bflags.value & self._BUFFLAGS_SILENT) or not pdata.value:
                                    arr = np.zeros((n, self._ch), dtype=np.float32)
                                else:
                                    raw = (ctypes.c_byte * (n * frame_bytes)).from_address(pdata.value)
                                    arr = np.frombuffer(bytes(raw), dtype=np.float32).reshape(n, self._ch).copy()
                                self._cb(arr, n, None, None)
                                vc(cap, 4, _wt.HRESULT, _wt.UINT, n)
                        vc(cli, 11, _wt.HRESULT)  # IAudioClient::Stop
                    finally: self._release(cap)
                finally: self._release(cli)
            finally: self._release(dev)
        finally: self._release(enum)


def get_audio_outputs():
    # only WASAPI devices show up here; MME/DirectSound don't support loopback
    try:
        apis = sd.query_hostapis()
        wasapi = next((i for i, a in enumerate(apis) if 'wasapi' in a['name'].lower()), None)
        if wasapi is None:
            return []
        return [(i, d['name']) for i, d in enumerate(sd.query_devices())
                if d['hostapi'] == wasapi and d['max_output_channels'] > 0]
    except Exception:
        return []

def find_default_output(devices):
    try:
        apis = sd.query_hostapis()
        wasapi_idx = next((i for i, a in enumerate(apis) if 'wasapi' in a['name'].lower()), None)
        if wasapi_idx is None:
            return None
        default = apis[wasapi_idx].get('default_output_device', -1)
        if default >= 0:
            return default
        if devices:
            return devices[0][0]
        return None
    except Exception:
        return None

def scaled_res(monitor, res_label):
    nw, nh = monitor['width'], monitor['height']
    th = RES_H.get(res_label, nh)
    if nh <= th: return nw, nh
    scale = th / nh
    return int(nw * scale) & ~1, int(nh * scale) & ~1  # & ~1 keeps dims even, x264 requirement

def audio_callback(indata, frames, t, status):
    if not cap_stop.is_set():
        now = time.time()
        with buf_lock:
            aud_buf.append((now, indata.copy()))
            cutoff = now - KEEP_SECS
            while aud_buf and aud_buf[0][0] < cutoff:
                aud_buf.popleft()

def capture_loop(monitor, w, h, fps, stop_ev):
    interval = 1.0 / fps
    nxt = time.perf_counter()
    with mss.mss() as sct:  # keeping this open for the whole loop is way faster than per-frame
        while not stop_ev.is_set():
            now = time.time()
            frame = np.array(sct.grab(monitor))
            bgr = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
            if (bgr.shape[1], bgr.shape[0]) != (w, h):
                bgr = cv2.resize(bgr, (w, h), interpolation=cv2.INTER_AREA)
            ok, enc = cv2.imencode('.jpg', bgr, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if ok:
                with buf_lock:
                    vid_buf.append((now, enc.tobytes()))
                    cutoff = now - KEEP_SECS
                    while vid_buf and vid_buf[0][0] < cutoff:
                        vid_buf.popleft()
            nxt += interval
            wait = nxt - time.perf_counter()
            if wait > 0:
                time.sleep(wait)

def _save(dur_secs, fps, w, h, out_dir, ffmpeg, on_done):
    global is_saving
    now = time.time()
    cutoff = now - dur_secs
    with buf_lock:
        frames  = [(t, d) for t, d in vid_buf if t >= cutoff]
        achunks = [(t, d) for t, d in aud_buf if t >= cutoff]

    if not frames:
        is_saving = False
        on_done(False, 'Nothing in the buffer yet — give it a few seconds after starting.')
        return

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp      = datetime.now().strftime('%Y%m%d_%H%M%S')
    audio_path = out / f'audio_{stamp}.wav'
    final_path = out / f'clip_{stamp}.mp4'

    has_audio = bool(achunks)
    if has_audio:
        arr = np.concatenate([c for _, c in achunks], axis=0)
        pcm = (np.clip(arr, -1.0, 1.0) * 32767).astype(np.int16)
        with wave.open(str(audio_path), 'wb') as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm.tobytes())

    # mss grab latency is variable so we measure real fps from timestamps rather
    # than trusting the target; ffmpeg then duplicates frames to hit the target rate
    # print(f'[dbg] {len(frames)} frames  {len(achunks)} audio chunks  actual={actual_fps:.2f}fps')
    if len(frames) >= 2:
        span = frames[-1][0] - frames[0][0]
        actual_fps = round((len(frames) - 1) / span, 6) if span > 0 else fps
    else:
        actual_fps = fps
    actual_fps = max(1.0, actual_fps)

    cmd = [ffmpeg, '-y', '-f', 'image2pipe', '-framerate', str(actual_fps), '-i', 'pipe:0']
    if has_audio:
        cmd += ['-i', str(audio_path)]
    cmd += ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
            '-vf', f'fps={fps}']
    if has_audio:
        cmd += ['-c:a', 'aac', '-b:a', '192k', '-shortest']
    cmd.append(str(final_path))

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NO_WINDOW)

    # write frames in a thread so stdin doesn't block while we wait for ffmpeg stderr
    def _write_frames():
        try:
            for _, jpeg in frames:
                proc.stdin.write(jpeg)
        finally:
            proc.stdin.close()

    t = threading.Thread(target=_write_frames, daemon=True)
    t.start()
    stderr_data = proc.stderr.read()
    proc.wait()
    t.join()

    if has_audio and audio_path.exists():
        audio_path.unlink()

    is_saving = False
    if proc.returncode == 0:
        on_done(True, str(final_path))
    else:
        on_done(False, stderr_data.decode(errors='replace'))

_SAVE_SOUND_B64 = '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My4xMDCqqqqqTEFNRTMuMTAwqqqqqkxBTUUzLjEwMKqqqqpMQU1FMy4xMDCqqqqqTEFNRTMuMTAwqqqqqkxBTUUzLjEwMKqqqqpMQU1FMy4xMDCqqqqqTEFNRTMuMTAwqqqqqkxBTUUzLjEwMKqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqr/8xDE8AAAA0gBwAAAqqqqqqqqqqqqqqqqqv/zEMTyAAADSAAAAACqqqqqqqqqqqqqqqqq//MQxPIAAANIAAAAAKqqqqqqqqqqqqqqqqr/8xDE8gAAA0gAAAAAqqqqqqqqqqqqqqqqqv/zEMTyAAADSAAAAACqqqqqqqqqqqqqqqqq//MQxPIAAANIAAAAAKqqqqqqqqqqqqqqqqr/8xDE8gAAA0gAAAAAqqqqqqqqqqqqqqqqqv/zEMTyAAADSAAAAACqqqqqqqqqqqqqqqqq//MQxPIAAANIAAAAAKqqqqqqqqqqqqqqqqr/8xDE8gAAA0gAAAAAqqqqqqqqqqqqqqqqqv/zEMTyAAADSAAAAACqqqqqqqqqqqqqqqqq//MQxPIAAANIAAAAAKqqqqqqqqqqqqqqqqr/8xDE8gAAA0gAAAAAqqqqqqqqqqqqqqqqqv/zEMTyAAADSAAAAACqqqqqqqqqqqqqqqqq//MQxPIAAANIAAAAAKqqqqqqqqqqqqqqqqr/8xDE8gAAA0gAAAAAqqqqqqqqqqqqqqqqqv/zEMTyAAADSAAAAACqqqqqqqqqqqqqqqqq//MQxPIAAANIAAAAAKqqqqqqqqqqqqqqqqpUQUcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAyMDA1AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/w=='


def play_save_sound():
    import base64, tempfile, threading
    def _play():
        data = base64.b64decode(_SAVE_SOUND_B64)
        fd, path = tempfile.mkstemp(suffix='.mp3')
        try:
            os.write(fd, data)
            os.close(fd)
            mci = ctypes.windll.winmm.mciSendStringW
            alias = 'clipper_save_snd'
            mci(f'open "{path}" type mpegvideo alias {alias}', None, 0, None)
            mci(f'play {alias} wait', None, 0, None)
            mci(f'close {alias}', None, 0, None)
        except Exception:
            pass
        finally:
            try: os.unlink(path)
            except Exception: pass
    threading.Thread(target=_play, daemon=True).start()

BG      = '#b0d4e8'   # light water - matches card palette
CARD    = '#dff0fb'   # glass panel
CARD_B  = '#cce5f5'   # input / inner bg
BORDER  = '#72aad0'   # panel border
BORDER2 = '#96c6e4'   # widget border
ACCENT  = '#0078d4'   # windows blue
ACCENTG = '#5ab4ec'   # glow / highlight
TEXT    = '#0b1c2c'   # dark navy
TEXT2   = '#3a6880'   # blue-grey label
TBARBG  = '#3b85c0'   # title bar chrome
TBARHL  = '#62a8e0'   # title bar top highlight
FONT    = 'Segoe UI'

THEMES = {
    'water': {
        'BG': '#b0d4e8', 'CARD': '#dff0fb', 'CARD_B': '#cce5f5',
        'BORDER': '#72aad0', 'BORDER2': '#96c6e4',
        'ACCENT': '#0078d4', 'ACCENTG': '#5ab4ec',
        'TEXT': '#0b1c2c', 'TEXT2': '#3a6880',
        'TBARBG': '#3b85c0', 'TBARHL': '#62a8e0',
        'L1': '#a8ccdf', 'L2': '#98bcd5', 'L3': '#b8d8ea', 'L4': '#88b0cc',
        'SL': '#b0d4e8', 'SR': '#b0d4e8', 'SB': '#b8d8f0',
        'BUBBLE': '#c8e8f8', 'FOAM': '#dff0fb',
    },
}

ICON_B64 = 'iVBORw0KGgoAAAANSUhEUgAAAfQAAAH0CAYAAADL1t+KAAAgAElEQVR4Xux9B7gV1fU974HSBLsxlliiMZZgTIwae0GQGI0mf6OAoiK9SrEEO1JEEOnwAAFj74XYo4mJ+ktMjC12o8YkmthF+mv/tYbZ18Nw75u59d07s+b77nfvnTlzyjplnb3PPvtUtdAlBISAEBACQkAIVDwCVRVfAhVACAgBISAEhIAQaCFCVyMQAkJACAgBIRADBEToMahEFUEICAEhIASEgAhdbUAICAEhIASEQAwQEKHHoBJVBCEgBISAEBACInS1ASEgBISAEBACMUBAhB6DSlQRhIAQEAJCQAiI0NUGhIAQEAJCQAjEAAERegwqUUUQAkJACAgBISBCVxsQAkJACAgBIRADBEToMahEFUEICAEhIASEgAhdbUAICAEhIASEQAwQEKHHoBJVBCEgBISAEBACInS1ASEgBISAEBACMUBAhB6DSlQRhIAQEAJCQAiI0NUGhIAQEAJCQAjEAAERegwqUUUQAkJACAgBISBCVxsQAkJACAgBIRADBEToMahEFUEICAEhIASEgAhdbUAICAEhIASEQAwQEKHHoBJVBCEgBISAEBACInS1ASEgBISAEBACMUBAhB6DSlQRhIAQEAJCQAiI0NUGhIAQEAJCQAjEAAERegwqUUUQAkJACAgBISBCVxsQAkJACAgBIRADBEToMahEFUEICAEhIASEgAhdbUAICAEhIASEQAwQEKHHoBJVBCEgBISAEBACInS1ASEgBISAEBACMUBAhB6DSlQRhIAQEAJCQAiI0NUGhIAQEAJCQAjEAAERegwqUUUQAkJACAgBISBCVxsQAkJACAgBIRADBEToMahEFUEICAEhIASEgAhdbUAICAEhIASEQAwQEKHHoBJVBCEgBISAEBACInS1ASEgBISAEBACMUBAhB6DSlQRhIAQEAJCQAiI0NUGhIAQEAJCQAjEAAERegwqUUUQAkJACAgBISBCVxsQAkJACAgBIRADBEToMahEFUEICAEhIASEgAhdbUAICAEhIASEQAwQEKHHoBJVBCEgBISAEBACInS1ASEgBISAEBACMUBAhB6DSlQRhIAQEAJCQAiI0NUGhIAQEAJCQAjEAAERegwqUUUQAkJACAgBISBCVxsQAkJACAgBIRADBEToMahEFUEICAEhIASEgAhdbUAICAEhIASEQAwQEKHHoBJVBCEgBISAEBACInS1ASEgBISAEBACMUBAhB6DSlQRhIAQEAJCQAiI0NUGhIAQEAJCQAjEAAERegwqUUUQAkJACAgBISBCVxsQAkJACAgBIRADBEToMahEFUEICAEhIASEQGIJvbGxkWWvTtMEGv17icUmx25huLWoqqpqyCYO1IVbD8I9G/CSEzbVvtIUOds201RcjD7b+PhOYzbtPjD+WH743VTaTCMs78lpESrpBgjk0nArHsbbb7990yeffPKAli1b7oIO0qaurq4avxsbGhpqq6ur61jANWvWVK9evbplq1atqtj57NvruesmA95lHcztaIivkeH5vLa21guHuFsgbu9dhu3YsWOD/5v/65F+LcJsjHfx6rp3nfjXMF943grPN3bTZ7y8GLfl4auvvkoRJMvVVD7tWXCgsDTcb/7eeOONG9u1a0esmB6x4W/mv57lwN+vdthhh2fPO++8d8MaCsK2vOiii763atWqverr61ujDC3xuxXiqw6my7jcOnDjZjaIuVsGP2upulu7dm1V69atW7Rp04ZZrkJ667V9wykYr/ufLxpelh7T4Yf1zHbEb36Q1nqTGsu7tQeLl3jab7derZ3xHj/M90YbbeS1H9a11buVmfkJtskw/N10V6xYgWi/bkPMJ9OzfATjsnTdPFu7tTzzm/lke7G2b/FYf/DjT+Wd9eDXXSPqqIG/8a73zYv33PaM8NVo76l+GsQw2L7dekMeUukiDa89WN5ZFrZD4s402MVYv36b98IyX/xw3ECYVW3btn12woQJr0Uh3WnTpnV64403DmJ/R5prgPUavLea/RwXx6T2TJ/psH8BwzWok8+6dev27M9//vNPo9atwiULgcQROjvs0KFDj5o1a9Z0VPVu+LTEh/2UnZmd1Bt80KGMsNYb45toHmlnzujo3is2gGFA8v5jwGAaHv74XYeBfRVIpx3urXvh66sRz1bjww5Nwm8bJCPmnXlm3AiXmkT4A1SmLEea6TP/yLtHIpZ33rPfTJv//YGSk5Sv9ttvvynPP//8VWFd6W9/+9vWRx111Oxly5Ydh/c2RlwpIndJi/UTFheepy2Pn78q1ifjtLq1+Pg8x8tLz28z3rfP96nJFeNmupnyb2nbe+nyYVhjwPfIkbgH8XfbSrZlsfojgfuk60XB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trUjOSDYtXr1ak/dmY3KU3USXwSc074aQA733XvvvSOOPvro9wu1HgtSrZo7d+53Lrrooqs///zznyDeVnYOeFRUsyV0LEtNgXvk4ZVO6JnOUzcnNNy/TmwYDt8NK1as+Oq73/3ug+PHj78cff+tQtVh1HqKe7hEEvqoUaN6YA29BoShNZsSDsAAACAASURBVPQ8W7izpchiqt93333nvPjii5GM4qByfxDb1r7PAWDNmjVex8capqfuXLZsWZ650+txQIBtjMTga24+xPrz3JEjRy457bTT/l0IQoAafztshzv3lVde6Q2C3QJtryrqZNI14szmtDWUYfJf/vIX2plU9La1YP93lylYZ5TOMUkyQiepN6Kf12JL2y3YVTAe6ve3C1GHcWjnhShDIgkda3A9MCOvAYAi9EK0Ij8Odm586rGGPhfSx9CwqF9++eVvgNBv/9///nc4w5qqndteSOwmqYfFo+fxRcA9GMSX1OvhjezDXXbZ5fHOnTvfeNxxx73+5ZdfotlVteTHkACZpKypsY7byP9oVzTQaly+fLn3G6S90R133LH3q6++2uMf//jHsXh3S3wYF/e8e2vDwaNDm0J6iy22iHweOo5YnYTTBkciDxVN6EE8XAM5Tsw5MWK/3mSTTbx1dl/zRuv3lVC/3zp16tQJxx9//Lsi9cL04UQSOs9DB6HPp4QedSZeGLjjGYs7SyehQ0KfF2Uf+ttvv70N/F7f/sEHHxzBAdTqgvG1bt06NRjEEzWVKhsETIXrr6eTrOvwWQECWUUGplQN0q6m+txckrrey5gWWYRfJA+QC92UVuN3G9xuj/stGQfbnpF4FKM4I3/GTwn9scce64L18Q/DyoZTBic+/fTToytdQg+W050MtWvXrgVU7F4Q8/fO3369sC5WY5/6nVC/T9DRq2EtJtrzxBI6D2fxO3I0pBQqIwLuoIZAlNAjEfo777zzjYMOOughGMXtx8htrZTETimd/1euXCnkE4yAK6Hbb/dAEF8r5CFEvjYDOv531eHufWcbnNfGzJERw7PtWdgoEnqA0CMbxR1wwAHjn3322fPjQOiGoYsx64qEzmUzqt0ppXNJza0TSO3Ulizbc889l47FBVL/B57r6NU8+nsiCR370LuT0DETb+82wjxw1KtfI1CP89AjqdxJ6AcffPAD//3vf3/I17nmZsZI9ttm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BtSA3iu77777o2+++eZP8WxDd1BRIlUYIRBjBNi39thjj6XvvPNON5BZVlpS628gxHqq3GEUFyqh+4TO89APiDJJLxT0TMvdluobPXpr53xGIqd07k5q8smfs+PmqwEDBvTBeeg6PrVQlVmu8bAznXPOOd25Dz0JEjrrgRL6r3/960gSOozYroAa72Jaav//9q4E2K6iTJsESFgFiyWAjCC4jCLIjGjJopgECEIQix2kBCGQhJCQFQSVQCKYjSwmZAEHWcQNEXCMEAgCAWosaxy1ygXZAkFHpIyGERJC8t5838n5r/06594+5y4vZ/lu1at73+k+3X9/f3d//ffyd9rG5a+jHXzwwct+/etfn4znm/JaDySXENhaCHDa/bDDDvvBL3/5y1MgQ8cJHUdW98J1oj/CevbhVma2WRJgipMALcNk1rgRt+Xr72xP29+4AtWZcheht6y1giRghI41dDqWKf2Ue1ZCdy30ek5ifFXb1Jk1SOxK/t299977yU984hOvFqRaSEwh0GsIYLC729FHH/0QdvH/W7OEzjV0tNVUFjoJHVPQP0Z+/97pQrob49LmVe+dNASfFAfYkNCHL1y48HsID55oSStnEeJlGh0WoUAhGY3QsYa+BMresTdGqCGZOh3OKfe777578IknnvhiKC8SOi10xEs95W7rcrbjHe+uHzt27Cnz5s17MJSfwoVA1RCYOHHikFmzZt2DgfDOWT2kGYGR0DEouPaxxx6bGsIPm+IGYt/QMljohzVDuKH0/XDXavYdLzGua6nbmjq//al3N24aGZzlv3/ghM3wpUuXktArtSm3koRO16+8Dx2VpPQWOis5LkF59p577hmM6fSXQg1j6NChU7Ap7suxk46gYxnXijdCp6ONPffc8z+vueYaGAYj/xjKU+FCoCoI3HLLLe9Cu5iP8+3DMjhKqsFj7Y3LWZgBy0LoPwGhf7gThO6n2cjiTiLptO/XqyMJVvo/aKFjDV2EXvaGRQt93LhxZ8Jx/1KMjivhyx27zp/lsbVTTz01aKEfeeSRM5588smJqAepB3vWIK3uxCPtDXvttdcDo0ePnoOBxC8+9rGP2Vn0mvvLOnWt7yuvvNIH79aSi3/0mDpbtWpV7f/999/fn1bzPXoxCbc8rgxZp+QScYE8b4McURhlc2SqeaVrQ9vq4T60DelF/WsscyS7h2US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ARF67lUkAYWAEBACQkAIhBEQoYcxUgwhIASEgBAQArlHQISeexVJQCEgBISAEBACYQRE6GGMFEMICAEhIASEQO4REKHnXkUSUAgIASEgBIRAGAERehgjxRACQkAICAEhkHsEROi5V5EEFAJCQAgIASEQRkCEHsZIMYSAEBACQkAI5B4BEXruVSQBhYAQEAJCQAiEERChhzFSDCEgBISAEBACuUdAhJ57FUlAISAEhIAQEAJhBEToYYwUQwgIASEgBIRA7hEQoedeRRJQCAgBISAEhEAYARF6GCPFEAJCQAgIASGQewRE6LlXkQQUAkJACAgBIRBGQIQexkgxhIAQEAJCQAjkHgEReu5VJAGFgBAQAkJACIQREKGHMVIMISAEhIAQEAK5R0CEnnsVSUAhIASEgBAQAmEEROhhjBRDCAgBISAEhEDuERCh515FElAICAEhIASEQBgBEXoYI8UQAkJACAgBIZB7BETouVeRBBQCQkAICAEhEEZAhB7GSDGEgBAQAkJACOQeARF67lUkAYWAEBACQkAIhBEQoYcxUgwhIASEgBAQArlHQISeexVJQCEgBISAEBACYQRE6GGMFEMICAEhIASEQO4REKHnXkUSUAgIASEgBIRAGAERehgjxRACQkAICAEhkHsEROi5V5EEFAJCQAgIASEQRkCEHsZIMYSAEBACQkAI5B4BEXruVSQBhYAQEAJCQAiEERChhzFSDCEgBISAEBACuUdAhJ57FUlAISAEhIAQEAJhBEToYYwUQwgIASEgBIRA7hEQoedeRRJQCAgBISAEhEAYARF6GCPFEAJCQAgIASGQewRE6LlXkQQUAkJACAgBIRBGQIQexkgxhIAQEAJCQAjkHgEReu5VJAGFgBAQAkJACIQREKGHMVIMISAEhIAQEAK5R0CEnnsVSUAhIASEgBAQAmEEROhhjBRDCAgBISAEhEDuERCh515FElAICAEhIASEQBgBEXoYI8UQAkJACAgBIZB7BETouVeRBBQCQkAICAEhEEZAhB7GSDGEgBAQAkJACOQeARF67lUkAYWAEBACQkAIhBEQoYcxUgwhIASEgBAQArlHQISeexVJQCEgBISAEBACYQRE6GGMFEMICAEhIASEQO4REKHnXkUSUAgIASEgBIRAGAERehgjxRACQkAICAEhkHsEROi5V5EEFAJCQAgIASEQRkCEHsZIMYSAEBACQkAI5B6B/wecCjN28PBdTwAAABBkZUJHQzEyNUI3QzIxNDFCMUI2MvHsWxwAAAAASUVORK5CYII='



class WaveBar(tk.Canvas):
    def __init__(self, parent, h=14, **kw):
        super().__init__(parent, height=h, bg=CARD, highlightthickness=0, **kw)
        self._v = 0; self._m = 1
        self._display_v = 0.0
        self._phase = 0.0
        self._bubbles = []
        self.bind('<Configure>', lambda e: self._draw())
        self._tick()

    def _tick(self):
        import random, math
        app = self.winfo_toplevel()
        active = getattr(app, '_anim_active', True)

        # smooth easing — always runs regardless of focus/drag
        self._display_v += (self._v - self._display_v) * 0.20
        self._draw()

        # decorative effects only when focused and not dragging
        if active:
            self._phase += 0.10
            w = self.winfo_width(); h = self.winfo_height()
            fw = int(w * self._display_v / max(self._m, 1)) if self._m else 0
            if fw > 20 and random.random() < 0.20:
                self._bubbles.append({
                    'x': random.uniform(6, max(fw - 4, 7)),
                    'y': float(h - 2),
                    'r': random.uniform(1.5, 3.0),
                    'spd': random.uniform(0.5, 1.2),
                })
            self._bubbles = [b for b in self._bubbles if b['y'] > 1]
            for b in self._bubbles:
                b['y'] -= b['spd']
            if len(self._bubbles) > 8:
                self._bubbles = self._bubbles[-8:]

        self.after(45, self._tick)

    def set(self, val, maximum=1):
        prev = self._v
        self._v = min(val, maximum); self._m = max(maximum, 1)
        if prev > self._v:  # reset — snap display immediately
            self._display_v = self._v

    def _draw(self):
        import math
        self.delete('all')
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4: return
        self._rr(0, 0, w, h, '#c4e4f5')
        self.create_line(1, 1, w-1, 1, fill='#9cc8e0')
        dv = getattr(self, '_display_v', self._v)
        if self._m and dv:
            fw = int(w * dv / self._m)
            if fw >= h:
                self._rr(0, 0, fw, h, ACCENT)
                wh = max(2, h // 3)
                pts = [0, h]
                for x in range(0, fw + 2, 2):
                    wy = wh + int(2.5 * math.sin(x * 0.22 + self._phase))
                    pts += [x, max(1, wy)]
                pts += [fw, h]
                if len(pts) >= 6:
                    self.create_polygon(pts, fill='#3a9de0', outline='', smooth=False)
                for b in self._bubbles:
                    if b['x'] < fw - 1:
                        r = b['r']
                        self.create_oval(b['x']-r, b['y']-r, b['x']+r, b['y']+r,
                                         outline='#90d0f8', fill='#c8eeff', width=1)
                if fw > h + 4:
                    self._rr(0, 0, fw, h // 2, '#58b0e8')
                    self.create_line(h//2, 2, fw - h//2, 2, fill='#b0e0ff', width=1)
        self.create_line(1, 1, w - 1, 1, fill='#e8f8ff', width=1)

    def _rr(self, x1, y1, x2, y2, c):
        h = y2 - y1
        self.create_arc(x1, y1, x1+h, y2, start=90, extent=180, fill=c, outline=c)
        mid1, mid2 = x1+h//2, x2-h//2
        if mid2 > mid1:
            self.create_rectangle(mid1, y1, mid2, y2, fill=c, outline=c)
        self.create_arc(x2-h, y1, x2, y2, start=270, extent=180, fill=c, outline=c)


def make_card(parent, padx=14, pady=12):
    outer = tk.Frame(parent, bg=BORDER, highlightthickness=0)
    outer.pack(fill='x', padx=12, pady=5)
    tk.Frame(outer, bg='#f0f8ff', height=3).pack(fill='x')  # thin highlight strip at top
    body = tk.Frame(outer, bg=CARD)
    body.pack(fill='both', expand=True)
    inner = tk.Frame(body, bg=CARD)
    inner.pack(fill='both', expand=True, padx=padx, pady=pady)
    return inner


def make_btn(parent, text, cmd, state='normal'):
    b = tk.Button(parent, text=text, command=cmd, state=state,
                  bg='#4a9ed5', fg='#ffffff',
                  activebackground='#5bb2ea', activeforeground='#ffffff',
                  disabledforeground='#9ac8e4',
                  relief='flat', bd=0, padx=12, pady=5, font=(FONT, 9, 'bold'),
                  highlightthickness=1, highlightbackground='#2878b8',
                  cursor='hand2')
    return b


def make_entry(parent, var, width=20):
    return tk.Entry(parent, textvariable=var, width=width,
                    bg='#f0f8ff', fg=TEXT, font=(FONT, 9),
                    relief='flat', bd=0, insertbackground=ACCENT,
                    highlightthickness=1, highlightbackground=BORDER2,
                    highlightcolor=ACCENT)


class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.overrideredirect(True)
        self.resizable(False, False)
        self.configure(bg=BG)
        _icon = tk.PhotoImage(data=ICON_B64)
        self.iconphoto(True, _icon)
        self._dx = self._dy = 0
        self._ffmpeg    = imageio_ffmpeg.get_ffmpeg_exe()
        self._monitors  = get_monitors()
        self._outputs   = get_audio_outputs()
        self._cfg       = load_config()
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
        s.configure('TCombobox', fieldbackground='#f0f8ff', background=CARD_B,
                    foreground=TEXT, arrowcolor=ACCENT, bordercolor=BORDER,
                    darkcolor=CARD_B, lightcolor='#f0f8ff',
                    selectbackground='#a8d8f4', selectforeground=TEXT,
                    padding=(6, 4))
        s.map('TCombobox',
              fieldbackground=[('readonly', '#f0f8ff'), ('focus', '#f0f8ff')],
              selectbackground=[('readonly', '#a8d8f4')],
              bordercolor=[('focus', ACCENT)])
        self.option_add('*TCombobox*Listbox.background', '#f0f8ff')
        self.option_add('*TCombobox*Listbox.foreground', TEXT)
        self.option_add('*TCombobox*Listbox.selectBackground', '#a8d8f4')
        self.option_add('*TCombobox*Listbox.selectForeground', TEXT)
        self.option_add('*TCombobox*Listbox.font', (FONT, 9))
        self.option_add('*TCombobox*Listbox.relief', 'flat')

    def _fix_win(self):
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetAncestor(self.winfo_id(), 2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)   # rounded corners
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(ctypes.c_int(0)), 4)   # no dark titlebar flash
            GWL_EXSTYLE = -20
            cur = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, (cur | 0x00040000) & ~0x00000080)
            self.withdraw(); self.deiconify()
            # iconphoto() alone doesn't fix the taskbar — need WM_SETICON with a real HICON
            import base64, struct, tempfile, os as _os2
            png = base64.b64decode(ICON_B64)
            w = struct.unpack('>I', png[16:20])[0]
            h = struct.unpack('>I', png[20:24])[0]
            ico = (struct.pack('<HHH', 0, 1, 1) +
                   struct.pack('<BBBBHHII', min(w, 255), min(h, 255), 0, 0, 1, 32, len(png), 22) +
                   png)
            fd, tmp = tempfile.mkstemp(suffix='.ico')
            _os2.write(fd, ico); _os2.close(fd)
            self._icon_tmp = tmp
            hicon = ctypes.windll.user32.LoadImageW(
                0, tmp, 1, 0, 0, 0x10 | 0x40)
            if hicon:
                ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 1, hicon)
                ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 0, hicon)
        except Exception:
            pass

    def _build_titlebar(self):
        tk.Frame(self, bg=TBARHL, height=2).pack(fill='x')
        self._tb = tk.Canvas(self, height=32, bg=TBARBG, highlightthickness=0, bd=0)
        self._tb.pack(fill='x')
        self._tb.create_text(14, 16, text='◉  Clipper', anchor='w',
                             fill='#d8eeff', font=(FONT, 9, 'bold'), tags='static')
        self._close_l = tk.Label(self._tb, text='✕', bg=TBARBG, fg='#c0daf4',
                                  font=(FONT, 10), padx=14, cursor='hand2', bd=0)
        self._min_l   = tk.Label(self._tb, text='⎯', bg=TBARBG, fg='#c0daf4',
                                  font=(FONT, 10), padx=14, cursor='hand2', bd=0)
        self._tb.create_window(0, 0, window=self._close_l, anchor='ne', tags='wclose')
        self._tb.create_window(0, 0, window=self._min_l,   anchor='ne', tags='wmin')
        def _pos(e=None):
            w = self._tb.winfo_width()
            self._tb.coords('wclose', w, 0)
            self._tb.coords('wmin',   w - 46, 0)
        self._tb.bind('<Configure>', _pos)
        self._close_l.bind('<Button-1>', lambda e: self._quit())
        self._close_l.bind('<Enter>', lambda e: self._close_l.config(bg='#c0392b', fg='white'))
        self._close_l.bind('<Leave>', lambda e: self._close_l.config(bg=TBARBG, fg='#c0daf4'))
        self._min_l.bind('<Button-1>', lambda e: self._minimize())
        self._min_l.bind('<Enter>', lambda e: self._min_l.config(bg='#2d6ea8'))
        self._min_l.bind('<Leave>', lambda e: self._min_l.config(bg=TBARBG))
        tk.Frame(self, bg='#2868a8', height=1).pack(fill='x')
        self._tb.bind('<ButtonPress-1>',
                      lambda e: (setattr(self, '_dx', e.x_root - self.winfo_x()),
                                 setattr(self, '_dy', e.y_root - self.winfo_y()),
                                 setattr(self, '_dragging', True),
                                 setattr(self, '_anim_active', False)))
        self._tb.bind('<ButtonRelease-1>',
                      lambda e: (setattr(self, '_dragging', False),
                                 setattr(self, '_anim_active', True)))
        self._tb.bind('<B1-Motion>',
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
        sc = make_card(self, pady=10)
        row = tk.Frame(sc, bg=CARD)
        row.pack(fill='x')
        self._dot_cv = tk.Canvas(row, width=10, height=10, bg=CARD, highlightthickness=0)
        self._dot_cv.pack(side='left', padx=(0, 7))
        self._dot_id = self._dot_cv.create_oval(1, 1, 9, 9, fill='#e03030', outline='')
        tk.Label(row, text='REC', bg=CARD, fg=ACCENT, font=(FONT, 9, 'bold')).pack(side='left')
        self._buf_var = tk.StringVar(value='')
        tk.Label(row, textvariable=self._buf_var, bg=CARD, fg=TEXT2, font=(FONT, 9)).pack(side='right')
        self._bar = WaveBar(sc)
        self._bar.pack(fill='x', pady=(8, 0))

        stc = make_card(self)
        G = {'sticky': 'w', 'padx': (0, 10), 'pady': 4}

        tk.Label(stc, text='Monitor', bg=CARD, fg=TEXT2, font=(FONT, 9)).grid(row=0, column=0, **G)
        friendly  = get_monitor_names()
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
        self._mon_cb.bind('<<ComboboxSelected>>', lambda _: self._auto_apply())

        tk.Label(stc, text='Resolution', bg=CARD, fg=TEXT2, font=(FONT, 9)).grid(row=1, column=0, **G)
        self._res_var = tk.StringVar(value=self._cfg['resolution'])
        res_cb = ttk.Combobox(stc, textvariable=self._res_var,
                              values=('360p', '720p', '1080p'), width=10, state='readonly')
        res_cb.grid(row=1, column=1, columnspan=2, sticky='w', pady=4)
        res_cb.bind('<<ComboboxSelected>>', lambda _: self._auto_apply())

        tk.Label(stc, text='FPS', bg=CARD, fg=TEXT2, font=(FONT, 9)).grid(row=2, column=0, **G)
        self._fps_var = tk.StringVar(value=str(self._cfg['fps']))
        fps_cb = ttk.Combobox(stc, textvariable=self._fps_var,
                              values=('15', '30', '60'), width=10, state='readonly')
        fps_cb.grid(row=2, column=1, columnspan=2, sticky='w', pady=4)
        fps_cb.bind('<<ComboboxSelected>>', lambda _: self._auto_apply())

        tk.Label(stc, text='Audio', bg=CARD, fg=TEXT2, font=(FONT, 9)).grid(row=3, column=0, **G)
        dev_names = [n for _, n in self._outputs]
        self._aud_var = tk.StringVar()
        self._aud_cb  = ttk.Combobox(stc, textvariable=self._aud_var,
                                     values=dev_names, width=30, state='readonly')
        self._aud_cb.grid(row=3, column=1, columnspan=2, sticky='ew', pady=4)
        saved = self._cfg.get('audio_name')
        self._aud_cb.current(dev_names.index(saved) if saved and saved in dev_names else 0)
        self._aud_cb.bind('<<ComboboxSelected>>', lambda _: self._auto_apply())

        tk.Label(stc, text='Clip Length', bg=CARD, fg=TEXT2, font=(FONT, 9)).grid(row=4, column=0, **G)
        self._dur_var = tk.StringVar(value=self._cfg['duration'])
        dur_cb = ttk.Combobox(stc, textvariable=self._dur_var,
                              values=list(DURATIONS.keys()), width=10, state='readonly')
        dur_cb.grid(row=4, column=1, columnspan=2, sticky='w', pady=4)
        dur_cb.bind('<<ComboboxSelected>>', lambda _: self._auto_apply())

        tk.Label(stc, text='Hotkey', bg=CARD, fg=TEXT2, font=(FONT, 9)).grid(row=5, column=0, **G)
        self._hk_var = tk.StringVar(value=self._cfg['hotkey'])
        hk_e = make_entry(stc, self._hk_var, width=18)
        hk_e.grid(row=5, column=1, sticky='w', pady=4)
        hk_e.bind('<FocusOut>', lambda _: self._auto_apply())
        hk_e.bind('<Return>',   lambda _: self._auto_apply())

        tk.Label(stc, text='Save To', bg=CARD, fg=TEXT2, font=(FONT, 9)).grid(row=6, column=0, **G)
        self._dir_var = tk.StringVar(value=self._cfg['output_dir'])
        dir_e = make_entry(stc, self._dir_var, width=22)
        dir_e.grid(row=6, column=1, sticky='ew', pady=4)
        dir_e.bind('<FocusOut>', lambda _: self._auto_apply())
        dir_e.bind('<Return>',   lambda _: self._auto_apply())
        make_btn(stc, 'Browse…', self._browse).grid(row=6, column=2, padx=(6, 0), pady=4)

        tk.Frame(stc, bg=BORDER2, height=1).grid(
            row=7, column=0, columnspan=3, sticky='ew', pady=(8, 6))

        bot = tk.Frame(stc, bg=CARD)
        bot.grid(row=8, column=0, columnspan=3, sticky='ew')
        self._topmost_var = tk.BooleanVar(value=self._cfg.get('topmost', False))
        tk.Checkbutton(bot, text='Always on top', variable=self._topmost_var,
                       bg=CARD, fg=TEXT, selectcolor='#a8d8f4',
                       activebackground=CARD, activeforeground=TEXT,
                       font=(FONT, 9), bd=0, highlightthickness=0,
                       cursor='hand2', command=self._toggle_topmost).pack(side='left')

        stc.columnconfigure(1, weight=1)

        ac = make_card(self, pady=10)
        brow = tk.Frame(ac, bg=CARD)
        brow.pack(fill='x')
        self._prev_btn = make_btn(brow, '▶  Preview Last Clip', self._preview, state='disabled')
        self._prev_btn.pack(side='left', padx=(0, 8))
        make_btn(brow, '📂  Open Clips Folder', self._open_folder).pack(side='left')
        self._sv = tk.StringVar(value='Starting…')
        tk.Label(ac, textvariable=self._sv, bg=CARD, fg=TEXT2,
                 font=(FONT, 8)).pack(anchor='w', pady=(7, 0))

        if self._topmost_var.get():
            self.wm_attributes('-topmost', True)

        self._anim_active = True
        self._dragging = False
        self._pulse()

    def _pulse(self):
        self._pb = not self._pb
        self._dot_cv.itemconfig(self._dot_id, fill='#e03030' if self._pb else '#b08080')
        self.after(750, self._pulse)

    def _auto_apply(self):
        if not hasattr(self, '_aud_cb'):
            return
        if hasattr(self, '_apply_job') and self._apply_job:
            self.after_cancel(self._apply_job)
        self._apply_job = self.after(500, self._apply)

    def _toggle_topmost(self):
        self.wm_attributes('-topmost', self._topmost_var.get())

    def _start(self):
        global cap_stop, vid_buf, aud_buf, KEEP_SECS
        cap_stop = threading.Event()
        with buf_lock:
            vid_buf.clear(); aud_buf.clear()

        mon_idx = self._mon_cb.current()
        monitor = self._monitors[max(mon_idx, 0)]
        fps     = int(self._fps_var.get())
        w, h    = scaled_res(monitor, self._res_var.get())
        self._cur_fps = fps
        self._cur_w   = w
        self._cur_h   = h

        # keep a couple extra seconds so the buffer is never short at the moment of save
        KEEP_SECS = DURATIONS[self._dur_var.get()] + 2

        sel = self._aud_cb.current()
        if not self._outputs:
            dev_idx = None
        else:
            dev_idx = self._outputs[max(sel, 0)][0]

        # output devices (speakers/headphones) need the loopback path;
        # actual input devices (mic) can go through sounddevice normally
        is_output = (dev_idx is not None and
                     sd.query_devices(dev_idx)['max_input_channels'] == 0)
        try:
            if is_output or dev_idx is None:
                self._stream = _WasapiLoopback(audio_callback, SAMPLE_RATE, CHANNELS)
            else:
                ch = min(CHANNELS, int(sd.query_devices(dev_idx)['max_input_channels'])) or CHANNELS
                self._stream = sd.InputStream(device=dev_idx, samplerate=SAMPLE_RATE,
                                              channels=ch, dtype='float32',
                                              callback=audio_callback)
            self._stream.start()
        except Exception as e:
            print(f'[audio] stream init failed: {e}')
            self._stream = None

        hk = self._hk_var.get().strip()
        try:
            keyboard.add_hotkey(hk, self._on_hotkey, suppress=True)
        except Exception as e:
            print(f'[hotkey] could not register {hk!r}: {e}')

        threading.Thread(target=capture_loop,
                         args=(monitor, w, h, fps, cap_stop), daemon=True).start()
        self._cap_start = time.time()
        self._sv.set(f'Press  {hk}  to clip the last {self._dur_var.get()}.')
        self._tick()

    def _stop(self):
        cap_stop.set()
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
            'audio_name': self._outputs[sel_aud][1] if self._outputs and 0 <= sel_aud < len(self._outputs) else None,
            'duration':   self._dur_var.get(),
            'hotkey':     self._hk_var.get().strip(),
            'output_dir': self._dir_var.get(),
            'topmost':    self._topmost_var.get(),
        })
        save_config(self._cfg)
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
        global is_saving
        if is_saving:
            return
        is_saving = True
        self._sv.set('Saving clip…')
        dur = DURATIONS[self._dur_var.get()]
        threading.Thread(target=_save, args=(dur, self._cur_fps, self._cur_w, self._cur_h,
            self._cfg['output_dir'], self._ffmpeg, self._on_saved), daemon=False).start()

    def _on_saved(self, ok, info): self.after(0, self._after_save, ok, info)

    def _after_save(self, ok, info):
        if ok:
            self._last_clip = info
            self._prev_btn.config(state='normal')
            self._sv.set('Saved  →  ' + Path(info).name)
            play_save_sound()
        else:
            self._sv.set('Save failed — check the console')
            print('[clipper] ffmpeg error:', info[:200] if len(info) > 200 else info)

    def _preview(self):
        if self._last_clip and Path(self._last_clip).exists():
            os.startfile(self._last_clip)

    def _open_folder(self):
        d = self._dir_var.get()
        Path(d).mkdir(parents=True, exist_ok=True)
        subprocess.Popen(['explorer', d])

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self._dir_var.get())
        if d: self._dir_var.set(d)

    def _quit(self):
        self._stop(); self.destroy()
        if hasattr(self, '_icon_tmp'):
            try:
                import os as _os3; _os3.unlink(self._icon_tmp)
            except Exception:
                pass


if __name__ == '__main__':
    App().mainloop()
