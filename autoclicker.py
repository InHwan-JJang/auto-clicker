"""오토 클릭커 - 지정한 화면 좌표를 사용자가 고른 간격(0~10000 ms)마다 클릭합니다.

* 실행하면 현재 마우스 좌표가 실시간으로 표시됩니다.
* 좌표(X, Y)와 클릭 간격(ms)을 정하고 [시작]을 누르면 그 좌표를 계속 클릭합니다.
* 단축키 (다른 프로그램 창이 활성 상태여도 동작)
      F6  : 현재 마우스 위치를 좌표 입력칸에 넣기
      F8  : 시작 / 정지
      ESC : 긴급 정지

빌드: build.bat (PyInstaller --onefile --windowed)
"""

import ctypes
import os
import queue
import re
import threading
import time
import tkinter as tk
import traceback
from ctypes import wintypes as wt
from tkinter import messagebox, ttk

APP_TITLE = "오토 클릭커"
MIN_INTERVAL_MS, MAX_INTERVAL_MS = 0, 10000
DEFAULT_INTERVAL_MS = 1000
SPIN_STEP_MS = 100          # 간격 입력칸의 화살표·마우스 휠 한 칸
SLIDER_STEP_MS = 50         # 슬라이더 눈금
SETTLE_S = 0.02             # 커서를 옮긴 뒤 대상 프로그램이 마우스 오버를 처리할 시간
HOLD_S = 0.02               # 누름-뗌 사이 간격 (없으면 클릭을 놓치는 프로그램이 있음)
RETRY_S = 0.1               # 클릭하지 못했을 때 다시 시도하기까지의 최소 대기
POLL_MS = 30                # 좌표 표시·상태·단축키 확인 주기
UI_FONT = "Malgun Gothic"
MONO_FONT = "Consolas"

# ── Win32 ────────────────────────────────────────────────────────────────
INPUT_MOUSE = 0
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
SM_SWAPBUTTON = 23
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
MONITOR_DEFAULTTONULL = 0
WM_QUIT, WM_HOTKEY = 0x0012, 0x0312
PM_NOREMOVE = 0
MOD_NOREPEAT = 0x4000
VK_ESCAPE, VK_F6, VK_F8 = 0x1B, 0x75, 0x77
HOTKEY_CAPTURE, HOTKEY_TOGGLE = 1, 2

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


def _api(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = restype, argtypes
    return fn


GetCursorPos = _api(user32, "GetCursorPos", wt.BOOL, ctypes.POINTER(wt.POINT))
SetCursorPos = _api(user32, "SetCursorPos", wt.BOOL, ctypes.c_int, ctypes.c_int)
SendInput = _api(user32, "SendInput", wt.UINT, wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
GetSystemMetrics = _api(user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
GetAsyncKeyState = _api(user32, "GetAsyncKeyState", ctypes.c_short, ctypes.c_int)
MonitorFromPoint = _api(user32, "MonitorFromPoint", wt.HMONITOR, wt.POINT, wt.DWORD)
WindowFromPoint = _api(user32, "WindowFromPoint", wt.HWND, wt.POINT)
GetWindowThreadProcessId = _api(user32, "GetWindowThreadProcessId", wt.DWORD,
                                wt.HWND, ctypes.POINTER(wt.DWORD))
RegisterHotKey = _api(user32, "RegisterHotKey", wt.BOOL, wt.HWND, ctypes.c_int, wt.UINT, wt.UINT)
UnregisterHotKey = _api(user32, "UnregisterHotKey", wt.BOOL, wt.HWND, ctypes.c_int)
GetMessageW = _api(user32, "GetMessageW", wt.BOOL,
                   ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
PeekMessageW = _api(user32, "PeekMessageW", wt.BOOL,
                    ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT)
PostThreadMessageW = _api(user32, "PostThreadMessageW", wt.BOOL,
                          wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM)
GetCurrentThreadId = _api(kernel32, "GetCurrentThreadId", wt.DWORD)


def enable_dpi_awareness():
    """좌표를 실제 픽셀 단위로 다루도록 DPI 인식을 켭니다. (Tk 창을 만들기 전에 호출)

    화면 배율(125%, 150% …)이 다른 모니터가 섞여 있어도 표시 좌표와 클릭 좌표가 일치합니다.
    """
    try:  # Windows 10 1703+ : Per-Monitor V2
        set_context = _api(user32, "SetProcessDpiAwarenessContext", wt.BOOL, ctypes.c_void_p)
        if set_context(ctypes.c_void_p(-4)):
            return
    except AttributeError:
        pass
    try:  # Windows 8.1+ : Per-Monitor
        ctypes.oledll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware()
    except AttributeError:
        pass


# ── 마우스 ───────────────────────────────────────────────────────────────
def get_cursor_pos():
    """현재 마우스 좌표 (x, y). 잠금 화면 등으로 읽을 수 없으면 None."""
    pt = wt.POINT()
    if not GetCursorPos(ctypes.byref(pt)):
        return None
    return pt.x, pt.y


def is_on_screen(x, y):
    """(x, y)가 실제로 어떤 모니터 위에 있는지."""
    left = GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = GetSystemMetrics(SM_YVIRTUALSCREEN)
    right = left + GetSystemMetrics(SM_CXVIRTUALSCREEN)
    bottom = top + GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if not (left <= x < right and top <= y < bottom):
        return False
    return bool(MonitorFromPoint(wt.POINT(x, y), MONITOR_DEFAULTTONULL))


def is_own_window_at(x, y):
    """(x, y)에 보이는 창이 이 프로그램의 창인지. (자기 자신을 클릭하지 않기 위해)"""
    hwnd = WindowFromPoint(wt.POINT(x, y))
    if not hwnd:
        return False
    pid = wt.DWORD()
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == os.getpid()


def _move_cursor(x, y, settle=True):
    if get_cursor_pos() == (x, y):
        return True
    if not SetCursorPos(x, y):
        return False
    if settle:
        time.sleep(SETTLE_S)
    return True


def _send_mouse(flags):
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi.dwFlags = flags
    return SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) == 1


def click_at(x, y):
    """(x, y)로 커서를 옮겨 주 버튼(보통 왼쪽)을 클릭합니다. 성공하면 True."""
    if not _move_cursor(x, y):
        return False
    # 왼손잡이 설정(버튼 바꿈)이면 SendInput의 '왼쪽'이 보조 버튼이 되므로 반대로 보냄
    if GetSystemMetrics(SM_SWAPBUTTON):
        down, up = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
    else:
        down, up = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
    pressed = _send_mouse(down)
    time.sleep(HOLD_S)
    _move_cursor(x, y, settle=False)   # 누르는 사이 사용자가 마우스를 움직였어도 제자리에서 뗌
    released = _send_mouse(up)
    return pressed and released


def describe_interval(ms):
    """간격(ms)을 사람이 읽기 쉬운 말로."""
    if ms >= 1000:
        return f"{ms / 1000:g}초마다 1번"
    fastest_ms = HOLD_S * 1000                 # 한 번 클릭하는 데 최소 HOLD_S가 걸림
    per_second = f"{1000 / max(ms, fastest_ms):.1f}".removesuffix(".0")
    return f"1초에 약 {per_second}번" + (" (최고 속도)" if ms <= fastest_ms else "")


# ── 백그라운드 작업 ──────────────────────────────────────────────────────
class GlobalHotkeys(threading.Thread):
    """RegisterHotKey로 전역 단축키를 등록하고, 눌린 단축키 id를 큐에 넣습니다."""

    def __init__(self, keys, events):
        super().__init__(daemon=True)
        self._keys = keys            # {단축키 id: 가상 키 코드}
        self._events = events
        self._thread_id = 0
        self.failed = []             # 다른 프로그램이 이미 쓰고 있어 등록하지 못한 id
        self.ready = threading.Event()

    def run(self):
        msg = wt.MSG()
        PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)   # 이 스레드의 메시지 큐 생성
        self._thread_id = GetCurrentThreadId()
        registered = []
        for hotkey_id, vk in self._keys.items():
            if RegisterHotKey(None, hotkey_id, MOD_NOREPEAT, vk):
                registered.append(hotkey_id)
            else:
                self.failed.append(hotkey_id)
        self.ready.set()
        try:
            while GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self._events.put(msg.wParam)
        finally:
            for hotkey_id in registered:
                UnregisterHotKey(None, hotkey_id)

    def stop(self):
        if self._thread_id:
            PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)


class ClickJob:
    """백그라운드 스레드에서 (x, y)를 interval_ms 간격으로 클릭합니다."""

    def __init__(self, x, y, interval_ms):
        self.x, self.y, self.interval_ms = x, y, interval_ms
        self.clicks = 0
        self.last_result = None      # "clicked" | "covered" | "failed"
        self.error = None
        self.next_click_at = time.monotonic() + interval_ms / 1000
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, wait=0.0):
        self._stop_event.set()
        self._thread.join(wait)

    def _run(self):
        interval = self.interval_ms / 1000
        try:
            while not self._stop_event.wait(max(0.0, self.next_click_at - time.monotonic())):
                if is_own_window_at(self.x, self.y):
                    self.last_result = "covered"
                elif click_at(self.x, self.y):
                    self.clicks += 1
                    self.last_result = "clicked"
                else:
                    self.last_result = "failed"
                # 다음 클릭은 '예정 시각 + 간격'으로 잡아 오차가 쌓이지 않게 하되,
                # 크게 밀렸으면(절전 등) 몰아서 클릭하지 않음
                now = time.monotonic()
                self.next_click_at += interval
                if self.next_click_at < now:
                    self.next_click_at = now + interval
                if self.last_result != "clicked":   # 클릭하지 못했으면 잠깐 쉬고 다시 시도
                    self.next_click_at = max(self.next_click_at, now + RETRY_S)
        except Exception:
            self.error = traceback.format_exc()


# ── 화면 ─────────────────────────────────────────────────────────────────
def _matcher(pattern):
    """입력칸 검증 함수: 입력 중인 문자열 전체가 pattern에 맞을 때만 허용."""
    regex = re.compile(pattern)
    return lambda text: regex.fullmatch(text) is not None


class App:
    def __init__(self, root):
        self.root = root
        self.job = None
        self._idle = ("대기 중", "좌표와 간격을 정하고 [시작]을 누르세요.")
        self._notice_text, self._notice_until = "", 0.0
        self._esc_was_down = False
        self._poll_id = None

        root.title(APP_TITLE)
        root.resizable(False, False)
        root.report_callback_exception = self._report_exception
        frame = self._build_ui()

        self.hotkey_events = queue.Queue()
        self.hotkeys = GlobalHotkeys({HOTKEY_CAPTURE: VK_F6, HOTKEY_TOGGLE: VK_F8},
                                     self.hotkey_events)
        self.hotkeys.start()
        self.hotkeys.ready.wait(1.0)
        self._show_hotkey_help(self.hotkeys.failed)

        # 상태 문구가 길어도 창 너비가 바뀌지 않도록 줄바꿈 폭을 고정
        root.update_idletasks()
        self.detail_label.configure(wraplength=frame.winfo_reqwidth() - 32)

        root.protocol("WM_DELETE_WINDOW", self.close)
        self._poll()

    def _build_ui(self):
        style = ttk.Style(self.root)
        style.configure(".", font=(UI_FONT, 10))
        style.configure("Head.TLabel", font=(UI_FONT, 10, "bold"))
        style.configure("Coord.TLabel", font=(MONO_FONT, 20, "bold"))
        style.configure("Hint.TLabel", font=(UI_FONT, 9), foreground="#555555")
        style.configure("Big.TButton", font=(UI_FONT, 11, "bold"), padding=(8, 6))
        coord_ok = (self.root.register(_matcher(r"-?[0-9]{0,5}")), "%P")
        ms_ok = (self.root.register(_matcher(r"[0-9]{0,5}")), "%P")

        frame = ttk.Frame(self.root, padding=16)
        frame.grid()

        self._heading(frame, "현재 마우스 좌표", row=0)
        self.cursor_var = tk.StringVar(value=self._format_pos(get_cursor_pos()))
        ttk.Label(frame, textvariable=self.cursor_var, style="Coord.TLabel").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(2, 8))
        ttk.Separator(frame).grid(row=2, column=0, columnspan=4, sticky="ew", pady=(0, 10))

        self._heading(frame, "클릭할 좌표", row=3)
        self.x_var, self.y_var = tk.StringVar(), tk.StringVar()
        self.x_entry = self._coord_entry(frame, "X", self.x_var, coord_ok, column=0)
        self.y_entry = self._coord_entry(frame, "Y", self.y_var, coord_ok, column=2)

        # 클릭 간격: 직접 입력(화살표·마우스 휠 가능) + 슬라이더, 둘은 서로 연동
        self._heading(frame, f"클릭 간격 ({MIN_INTERVAL_MS} ~ {MAX_INTERVAL_MS} ms)",
                      row=5, pady=(10, 0))
        interval_row = ttk.Frame(frame)
        interval_row.grid(row=6, column=0, columnspan=4, sticky="w", pady=3)
        self.interval_var = tk.StringVar(value=str(DEFAULT_INTERVAL_MS))
        self.interval_box = ttk.Spinbox(
            interval_row, textvariable=self.interval_var, from_=MIN_INTERVAL_MS,
            to=MAX_INTERVAL_MS, increment=SPIN_STEP_MS, width=7, justify="right",
            font=(UI_FONT, 10), validate="key", validatecommand=ms_ok)
        self.interval_box.grid(row=0, column=0)
        self.interval_box.bind("<Return>", lambda _event: self.start())
        ttk.Label(interval_row, text="ms").grid(row=0, column=1, padx=(4, 10))
        self.interval_desc_var = tk.StringVar()
        ttk.Label(interval_row, textvariable=self.interval_desc_var, style="Hint.TLabel").grid(
            row=0, column=2, sticky="w")
        self.slider_var = tk.DoubleVar(value=DEFAULT_INTERVAL_MS)
        self.slider = ttk.Scale(frame, from_=MIN_INTERVAL_MS, to=MAX_INTERVAL_MS,
                                variable=self.slider_var, command=self._on_slider)
        self.slider.grid(row=7, column=0, columnspan=4, sticky="ew")
        self.interval_var.trace_add("write", self._on_interval_typed)
        self._on_interval_typed()

        buttons = ttk.Frame(frame)
        buttons.grid(row=8, column=0, columnspan=4, sticky="ew", pady=(14, 10))
        buttons.columnconfigure((0, 1), weight=1, uniform="btn")
        self.start_btn = ttk.Button(buttons, text="▶ 시작 (F8)", style="Big.TButton",
                                    command=self.start)
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.stop_btn = ttk.Button(buttons, text="■ 정지 (F8)", style="Big.TButton",
                                   command=self.stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        self.status_var, self.detail_var = tk.StringVar(), tk.StringVar()
        ttk.Label(frame, textvariable=self.status_var, style="Head.TLabel").grid(
            row=9, column=0, columnspan=4, sticky="w")
        # 높이를 2줄로 고정해 문구가 바뀌어도 창 크기가 흔들리지 않게 함
        self.detail_label = tk.Label(
            frame, textvariable=self.detail_var, font=(UI_FONT, 9), fg="#444444",
            bg=style.lookup("TFrame", "background") or "SystemButtonFace",
            anchor="nw", justify="left", height=2)
        self.detail_label.grid(row=10, column=0, columnspan=4, sticky="ew")

        ttk.Separator(frame).grid(row=11, column=0, columnspan=4, sticky="ew", pady=8)
        self.topmost_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="항상 위에 표시", variable=self.topmost_var,
                        command=self._apply_topmost).grid(row=12, column=0, columnspan=4, sticky="w")
        self.hint_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.hint_var, style="Hint.TLabel", justify="left").grid(
            row=13, column=0, columnspan=4, sticky="w", pady=(6, 0))

        self._apply_topmost()
        return frame

    def _heading(self, parent, text, row, pady=0):
        ttk.Label(parent, text=text, style="Head.TLabel").grid(
            row=row, column=0, columnspan=4, sticky="w", pady=pady)

    def _coord_entry(self, parent, label, var, validate, column):
        ttk.Label(parent, text=label).grid(row=4, column=column, sticky="e",
                                           padx=(12 if column else 0, 6), pady=3)
        entry = ttk.Entry(parent, textvariable=var, width=8, justify="right",
                          font=(UI_FONT, 10), validate="key", validatecommand=validate)
        entry.grid(row=4, column=column + 1, sticky="w", pady=3)
        entry.bind("<Return>", lambda _event: self.start())
        return entry

    def _show_hotkey_help(self, failed):
        def line(key, hotkey_id, text):
            return f"{key} : " + ("(다른 프로그램이 사용 중이라 사용 불가)" if hotkey_id in failed else text)

        self.hint_var.set("단축키 - 다른 창이 활성 상태여도 동작\n"
                          + line("F6 ", HOTKEY_CAPTURE, "현재 마우스 위치를 좌표칸에 입력") + "\n"
                          + line("F8 ", HOTKEY_TOGGLE, "시작 / 정지") + "\n"
                          + "ESC : 긴급 정지")

    @staticmethod
    def _format_pos(pos):
        if pos is None:
            return "X      -   Y      -"
        return f"X {pos[0]:>6}   Y {pos[1]:>6}"

    # ── 간격 입력칸 ↔ 슬라이더 ───────────────────────────────────────────
    def _interval_ms(self):
        try:
            ms = int(self.interval_var.get())
        except ValueError:
            return None
        return ms if MIN_INTERVAL_MS <= ms <= MAX_INTERVAL_MS else None

    def _on_interval_typed(self, *_):
        ms = self._interval_ms()
        if ms is None:
            self.interval_desc_var.set(f"{MIN_INTERVAL_MS}~{MAX_INTERVAL_MS} 사이 숫자")
            return
        self.slider_var.set(ms)
        self.interval_desc_var.set("= " + describe_interval(ms))

    def _on_slider(self, value):
        ms = round(float(value) / SLIDER_STEP_MS) * SLIDER_STEP_MS
        self._set_text(self.interval_var, str(ms))

    # ── 동작 ─────────────────────────────────────────────────────────────
    def start(self):
        if self.job:
            return
        target = self._read_inputs()
        if target is None:
            return
        self.job = ClickJob(*target)
        self._set_running(True)

    def stop(self):
        job, self.job = self.job, None
        if job is None:
            return
        job.stop(wait=0.2)
        self._idle = (f"정지됨 · 총 {job.clicks}회 클릭", "다시 시작하려면 [시작] 또는 F8을 누르세요.")
        self._set_running(False)

    def toggle(self):
        if self.job:
            self.stop()
        else:
            self.start()

    def capture_cursor(self):
        if self.job:
            self._notice("실행 중에는 좌표를 바꿀 수 없습니다. 먼저 정지하세요(F8).")
            return
        pos = get_cursor_pos()
        if pos is None:
            return
        self.x_var.set(str(pos[0]))
        self.y_var.set(str(pos[1]))
        self._notice(f"현재 위치 ({pos[0]}, {pos[1]})를 입력했습니다.")

    def close(self):
        if self.job:
            self.job.stop()
        self.hotkeys.stop()
        if self._poll_id:
            self.root.after_cancel(self._poll_id)
        self.root.destroy()

    def _read_inputs(self):
        try:
            x = int(self.x_var.get())
            y = int(self.y_var.get())
        except ValueError:
            self._warn("X, Y 좌표를 정수로 입력하세요.\n(F6을 누르면 현재 마우스 위치가 입력됩니다)")
            return None
        if not is_on_screen(x, y):
            self._warn(f"({x}, {y})는 모니터 화면 밖의 좌표입니다.")
            return None
        interval_ms = self._interval_ms()
        if interval_ms is None:
            self._warn(f"클릭 간격은 {MIN_INTERVAL_MS} ~ {MAX_INTERVAL_MS} ms 사이의 숫자로 입력하세요.")
            return None
        return x, y, interval_ms

    def _set_running(self, running):
        state = "disabled" if running else "normal"
        for widget in (self.x_entry, self.y_entry, self.interval_box, self.start_btn):
            widget.configure(state=state)
        self.slider.state(["disabled" if running else "!disabled"])
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.root.title(f"{APP_TITLE} - 실행 중" if running else APP_TITLE)

    def _apply_topmost(self):
        self.root.attributes("-topmost", self.topmost_var.get())

    def _notice(self, text, seconds=2.5):
        self._notice_text, self._notice_until = text, time.monotonic() + seconds

    def _warn(self, message):
        messagebox.showwarning(APP_TITLE, message, parent=self.root)
        self._drain_hotkeys()   # 대화상자가 떠 있는 동안 눌린 단축키는 무시

    def _drain_hotkeys(self):
        while True:
            try:
                self.hotkey_events.get_nowait()
            except queue.Empty:
                return

    def _report_exception(self, exc, value, tb):
        details = "".join(traceback.format_exception(exc, value, tb))
        messagebox.showerror(APP_TITLE, "예기치 못한 오류가 발생했습니다.\n\n" + details[-1500:],
                             parent=self.root)

    # ── 주기적 갱신 ──────────────────────────────────────────────────────
    def _poll(self):
        try:
            self._set_text(self.cursor_var, self._format_pos(get_cursor_pos()))
            esc_down = bool(GetAsyncKeyState(VK_ESCAPE) & 0x8000)
            if esc_down and not self._esc_was_down and self.job:
                self.stop()
                self._notice("ESC를 눌러 정지했습니다.")
            self._esc_was_down = esc_down
            while True:
                try:
                    hotkey = self.hotkey_events.get_nowait()
                except queue.Empty:
                    break
                if hotkey == HOTKEY_TOGGLE:
                    self.toggle()
                elif hotkey == HOTKEY_CAPTURE:
                    self.capture_cursor()
            self._render_status()
        finally:
            self._poll_id = self.root.after(POLL_MS, self._poll)

    def _render_status(self):
        if self.job and self.job.error:
            error = self.job.error
            self.stop()
            messagebox.showerror(APP_TITLE, "클릭 도중 오류가 발생해 정지했습니다.\n\n" + error[-1500:],
                                 parent=self.root)
        now = time.monotonic()
        job = self.job
        if job is None:
            status, detail = self._idle
        else:
            status = f"실행 중 · 클릭 {job.clicks}회"
            if job.last_result == "covered":
                detail = "※ 이 창이 대상 좌표를 가리고 있어 클릭하지 않았습니다. 창을 옮겨 주세요."
            elif job.last_result == "failed":
                detail = "※ 클릭 실패. 대상이 관리자 권한이면 이 프로그램도 관리자 권한으로 실행하세요."
            elif job.interval_ms >= 1000:
                remaining = max(0.0, job.next_click_at - now)
                detail = f"대상 ({job.x}, {job.y}) · 다음 클릭까지 {remaining:.1f}초"
            else:
                detail = f"대상 ({job.x}, {job.y}) · {job.interval_ms} ms 간격"
        if now < self._notice_until:
            detail = self._notice_text
        self._set_text(self.status_var, status)
        self._set_text(self.detail_var, detail)

    @staticmethod
    def _set_text(var, text):
        if var.get() != text:
            var.set(text)


def main():
    enable_dpi_awareness()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
