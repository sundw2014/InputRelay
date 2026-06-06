#!/usr/bin/env python3
"""
relay_sender.py -- Surface-side program for InputRelay (run this on the Surface).

** SAFE / MOUSE-TEST MODE **
Keyboard relay is currently DISABLED. The keyboard is never suppressed and never
sent -- it only listens for the toggle/quit hotkey -- so your Surface keyboard
always works locally and Ctrl+C in the terminal always kills the program. We will
re-enable keyboard relay once mouse capture is confirmed working.

What this relays right now:
  * Game controller (paired over Bluetooth) -- ALWAYS relayed.
  * Mouse -- TOGGLEABLE "capture mode". While ON, mouse movement/buttons/wheel
    drive the gaming PC and are blocked locally; while OFF, the mouse is normal.

Hotkeys:
  * Ctrl+Alt+End        -> toggle MOUSE capture on/off
  * Ctrl+Alt+Shift+End  -> quit the program
  * (and Ctrl+C in the terminal always works, since the keyboard is not grabbed)

Design fix vs. the first version: the low-level hook callback does NO blocking
work (no network sends, no beeps) -- it only updates shared state. A worker thread
sends accumulated mouse deltas, so the OS input pipeline never stalls.

Requires Windows. Controller part needs XInput-Python; mouse uses pure ctypes.
"""

import ctypes
import socket
import sys
import threading
import time
from ctypes import wintypes

import protocol as P

try:
    import winsound
except ImportError:
    winsound = None

# ----------------------- config -----------------------
TARGET_IP = "10.0.0.44"      # gaming PC running receiver.py
TARGET_PORT = 9999
KEEPALIVE_MS = 100           # resend held mouse-button state at least this often
MOUSE_FLUSH_MS = 2           # how often the worker flushes accumulated mouse motion
CONTROLLER_POLL_HZ = 500

HOTKEY_VK = 0x23             # VK_END  (Ctrl+Alt = toggle, +Shift = quit)
# ------------------------------------------------------

VK_CONTROL = 0x11
VK_MENU = 0x12     # ALT
VK_SHIFT = 0x10

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_QUIT = 0x0012

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C

LLMHF_INJECTED = 0x01
SM_CXSCREEN = 0
SM_CYSCREEN = 1
WHEEL_DELTA = 120

LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t

user32 = ctypes.windll.user32 if sys.platform == "win32" else None
kernel32 = ctypes.windll.kernel32 if sys.platform == "win32" else None
HOOKPROC = ctypes.CFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)

HHOOK = ctypes.c_void_p


def _setup_winapi():
    """Declare argtypes/restype so 64-bit pointers (lParam etc.) aren't truncated.
    Without this, ctypes treats args as 32-bit int and overflows on every event."""
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = HHOOK
    user32.CallNextHookEx.argtypes = [HHOOK, ctypes.c_int, WPARAM, LPARAM]
    user32.CallNextHookEx.restype = LRESULT
    user32.UnhookWindowsHookEx.argtypes = [HHOOK]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.c_void_p]
    user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
    user32.DispatchMessageW.restype = LRESULT
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, WPARAM, LPARAM]
    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = wintypes.SHORT
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD


if user32 is not None:
    _setup_winapi()


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


def key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def async_beep(on):
    if winsound:
        threading.Thread(target=winsound.Beep,
                         args=(1200 if on else 600, 80), daemon=True).start()


# ============================================================
# UDP sender
# ============================================================
class Sender:
    def __init__(self, ip, port):
        self.addr = (ip, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.lock = threading.Lock()
        self.seq = {P.T_GAMEPAD: 0, P.T_MOUSE: 0, P.T_KEYBOARD: 0, P.T_CONTROL: 0}

    def _next(self, t):
        s = self.seq[t]
        self.seq[t] = (s + 1) & 0xFF
        return s

    def gamepad(self, *args):
        with self.lock:
            self.sock.sendto(P.pack_gamepad(self._next(P.T_GAMEPAD), *args), self.addr)

    def mouse(self, dx, dy, wheel, buttons):
        with self.lock:
            self.sock.sendto(P.pack_mouse(self._next(P.T_MOUSE), dx, dy, wheel, buttons), self.addr)

    def control(self, subtype):
        with self.lock:
            self.sock.sendto(P.pack_control(self._next(P.T_CONTROL), subtype), self.addr)


# ============================================================
# Shared mouse state (written by hook, read by worker)
# ============================================================
class MouseState:
    def __init__(self):
        self.lock = threading.Lock()
        self.capture = False
        self.acc_dx = 0
        self.acc_dy = 0
        self.wheel = 0
        self.buttons = 0
        self.buttons_dirty = False


# ============================================================
# Hooks (own thread + message loop). Callback does NO blocking I/O.
# ============================================================
class HookThread(threading.Thread):
    def __init__(self, sender, mstate, on_quit):
        super().__init__(daemon=True)
        self.sender = sender
        self.m = mstate
        self.on_quit = on_quit
        self.thread_id = None
        self._kb_proc = HOOKPROC(self._keyboard_proc)
        self._ms_proc = HOOKPROC(self._mouse_proc)
        self._kb_hook = None
        self._ms_hook = None
        self.cx = user32.GetSystemMetrics(SM_CXSCREEN) // 2
        self.cy = user32.GetSystemMetrics(SM_CYSCREEN) // 2

    def _toggle(self):
        with self.m.lock:
            self.m.capture = not self.m.capture
            on = self.m.capture
            self.m.acc_dx = self.m.acc_dy = self.m.wheel = 0
            self.m.buttons = 0
            self.m.buttons_dirty = False
        if on:
            user32.SetCursorPos(self.cx, self.cy)
            print("\n[MOUSE CAPTURE ON]  (Ctrl+Alt+End to release)")
        else:
            self.sender.control(P.CTRL_RELEASE_ALL_KBM)
            print("\n[MOUSE CAPTURE OFF] mouse restored locally")
        async_beep(on)

    # keyboard: detect hotkey only; never suppress normal keys, never relay.
    # Wrapped so an exception can NEVER brick the keyboard (always passes through).
    def _keyboard_proc(self, nCode, wParam, lParam):
        try:
            if nCode == 0:
                kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if kb.vkCode == HOTKEY_VK and key_down(VK_CONTROL) and key_down(VK_MENU):
                    if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        if key_down(VK_SHIFT):
                            if self.thread_id is not None:
                                user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
                        else:
                            self._toggle()
                    return 1  # swallow only the hotkey itself
        except Exception as e:
            print("keyboard hook error (ignored):", e)
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    # mouse: only accumulate state; the worker thread sends.
    # Wrapped so an exception falls through to a normal (non-captured) mouse event.
    def _mouse_proc(self, nCode, wParam, lParam):
        try:
            if nCode == 0:
                with self.m.lock:
                    capturing = self.m.capture
                if capturing:
                    ms = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    if wParam == WM_MOUSEMOVE:
                        if not (ms.flags & LLMHF_INJECTED):
                            dx = ms.pt.x - self.cx
                            dy = ms.pt.y - self.cy
                            if dx or dy:
                                with self.m.lock:
                                    self.m.acc_dx += dx
                                    self.m.acc_dy += dy
                                user32.SetCursorPos(self.cx, self.cy)
                        return 1
                    if wParam == WM_MOUSEWHEEL:
                        delta = ctypes.c_short((ms.mouseData >> 16) & 0xFFFF).value
                        with self.m.lock:
                            self.m.wheel += delta // WHEEL_DELTA
                        return 1
                    btn = self._button_for(wParam, ms.mouseData)
                    if btn is not None:
                        down = wParam in (WM_LBUTTONDOWN, WM_RBUTTONDOWN,
                                          WM_MBUTTONDOWN, WM_XBUTTONDOWN)
                        with self.m.lock:
                            if down:
                                self.m.buttons |= btn
                            else:
                                self.m.buttons &= ~btn
                            self.m.buttons_dirty = True
                        return 1
        except Exception as e:
            print("mouse hook error (ignored):", e)
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    @staticmethod
    def _button_for(wParam, mouseData):
        if wParam in (WM_LBUTTONDOWN, WM_LBUTTONUP):
            return P.MB_LEFT
        if wParam in (WM_RBUTTONDOWN, WM_RBUTTONUP):
            return P.MB_RIGHT
        if wParam in (WM_MBUTTONDOWN, WM_MBUTTONUP):
            return P.MB_MIDDLE
        if wParam in (WM_XBUTTONDOWN, WM_XBUTTONUP):
            return P.MB_X1 if ((mouseData >> 16) & 0xFFFF) == 1 else P.MB_X2
        return None

    def run(self):
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        self._kb_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_proc, None, 0)
        self._ms_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_proc, None, 0)
        if not self._kb_hook or not self._ms_hook:
            print("ERROR: failed to install input hooks. Try running as Administrator.")
            self.on_quit()
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if self._kb_hook:
            user32.UnhookWindowsHookEx(self._kb_hook)
        if self._ms_hook:
            user32.UnhookWindowsHookEx(self._ms_hook)
        self.on_quit()


# ============================================================
# Mouse worker: flush accumulated motion/wheel/buttons over UDP
# ============================================================
def mouse_worker(sender, mstate, stop):
    interval = MOUSE_FLUSH_MS / 1000.0
    keepalive = KEEPALIVE_MS / 1000.0
    last_send = 0.0
    while not stop.is_set():
        time.sleep(interval)
        with mstate.lock:
            if not mstate.capture:
                continue
            dx, dy, wheel = mstate.acc_dx, mstate.acc_dy, mstate.wheel
            buttons = mstate.buttons
            dirty = mstate.buttons_dirty
            mstate.acc_dx = mstate.acc_dy = mstate.wheel = 0
            mstate.buttons_dirty = False
        now = time.time()
        if dx or dy or wheel or dirty:
            # clamp deltas to int16 range just in case
            dx = max(-32768, min(32767, dx))
            dy = max(-32768, min(32767, dy))
            wheel = max(-128, min(127, wheel))
            sender.mouse(dx, dy, wheel, buttons)
            last_send = now
        elif now - last_send >= keepalive:
            sender.mouse(0, 0, 0, buttons)
            last_send = now


# ============================================================
# Controller relay (always on)
# ============================================================
def clamp_i16(v):
    v = int(round(v))
    return 32767 if v > 32767 else -32768 if v < -32768 else v


def clamp_u8(v):
    v = int(round(v))
    return 255 if v > 255 else 0 if v < 0 else v


_GP_BUTTONS = [
    ("A", P.BTN_A), ("B", P.BTN_B), ("X", P.BTN_X), ("Y", P.BTN_Y),
    ("LEFT_SHOULDER", P.BTN_LB), ("RIGHT_SHOULDER", P.BTN_RB),
    ("BACK", P.BTN_VIEW), ("START", P.BTN_MENU),
    ("LEFT_THUMB", P.BTN_LS), ("RIGHT_THUMB", P.BTN_RS), ("GUIDE", P.BTN_GUIDE),
]
_GP_DPAD = [
    ("DPAD_UP", P.DP_UP), ("DPAD_DOWN", P.DP_DOWN),
    ("DPAD_LEFT", P.DP_LEFT), ("DPAD_RIGHT", P.DP_RIGHT),
]


def controller_loop(sender, stop):
    try:
        import XInput
    except ImportError:
        print("NOTE: XInput-Python not installed -> controller relay disabled "
              "(mouse still works). pip install XInput-Python to enable.")
        return

    XInput.set_deadzone(XInput.DEADZONE_LEFT_THUMB, 0)
    XInput.set_deadzone(XInput.DEADZONE_RIGHT_THUMB, 0)
    XInput.set_deadzone(XInput.DEADZONE_TRIGGER, 0)

    poll = 1.0 / CONTROLLER_POLL_HZ
    keepalive = KEEPALIVE_MS / 1000.0
    last_state = None
    last_send = 0.0
    have = False

    while not stop.is_set():
        conn = XInput.get_connected()
        idx = next((i for i, c in enumerate(conn) if c), None)
        if idx is None:
            if have:
                print("\nController disconnected.")
                have = False
            time.sleep(0.1)
            continue
        if not have:
            print(f"\nController {idx} connected.")
            have = True

        try:
            st = XInput.get_state(idx)
        except XInput.XInputNotConnectedError:
            have = False
            continue

        btns = XInput.get_button_values(st)
        buttons = 0
        for k, bit in _GP_BUTTONS:
            if btns.get(k):
                buttons |= bit
        dpad = 0
        for k, bit in _GP_DPAD:
            if btns.get(k):
                dpad |= bit
        lt_f, rt_f = XInput.get_trigger_values(st)
        (lx_f, ly_f), (rx_f, ry_f) = XInput.get_thumb_values(st)
        vals = (buttons, dpad,
                clamp_i16(lx_f * 32767), clamp_i16(ly_f * 32767),
                clamp_i16(rx_f * 32767), clamp_i16(ry_f * 32767),
                clamp_u8(lt_f * 255), clamp_u8(rt_f * 255))

        now = time.time()
        if vals != last_state or (now - last_send) >= keepalive:
            sender.gamepad(*vals)
            last_state = vals
            last_send = now
        time.sleep(poll)


# ============================================================
# Main
# ============================================================
def main():
    if sys.platform != "win32":
        sys.exit("relay_sender.py must run on Windows (uses Win32 hooks + XInput).")

    sender = Sender(TARGET_IP, TARGET_PORT)
    mstate = MouseState()
    stop = threading.Event()

    hooks = HookThread(sender, mstate, stop.set)
    ctrl = threading.Thread(target=controller_loop, args=(sender, stop), daemon=True)
    work = threading.Thread(target=mouse_worker, args=(sender, mstate, stop), daemon=True)

    print(f"InputRelay sender -> {TARGET_IP}:{TARGET_PORT}")
    print("Controller relay: ON (always).")
    print("Keyboard relay: DISABLED (safe mode -- keyboard stays local).")
    print("Mouse capture: OFF. Ctrl+Alt+End = toggle, Ctrl+Alt+Shift+End = quit, "
          "Ctrl+C also quits.")

    hooks.start()
    ctrl.start()
    work.start()

    try:
        while not stop.is_set():
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        sender.control(P.CTRL_RELEASE_ALL_KBM)
        if hooks.thread_id is not None:
            user32.PostThreadMessageW(hooks.thread_id, WM_QUIT, 0, 0)
        time.sleep(0.2)
        print("\nBye.")


if __name__ == "__main__":
    main()
