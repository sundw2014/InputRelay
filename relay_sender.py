#!/usr/bin/env python3
"""
relay_sender.py -- Surface-side program for InputRelay (run this on the Surface).

A single program that relays input to the gaming PC over UDP:

  * Game controller (paired to this PC over Bluetooth) -- ALWAYS relayed while
    the program runs. It never blocks using the Surface, so it stays on.

  * Keyboard + mouse -- a TOGGLEABLE "capture mode". While ON, every key and
    mouse movement drives the gaming PC and is blocked locally on the Surface
    (full takeover), except the reserved hotkeys. While OFF, the Surface behaves
    normally.

Hotkeys (configurable below):
  * Ctrl+Alt+End            -> toggle keyboard/mouse capture on/off
  * Ctrl+Alt+Shift+End      -> quit this program (always restores input)

On toggle, a short beep gives feedback (rising = ON, falling = OFF).

Requires Windows (uses Win32 low-level hooks + SendInput-style relay). The
controller part needs XInput-Python; keyboard/mouse use pure ctypes.

Setup:
    pip install XInput-Python
    edit TARGET_IP below, then:  python relay_sender.py
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
KEEPALIVE_MS = 100           # resend held state at least this often
CONTROLLER_POLL_HZ = 500

# Hotkeys: trigger key + required modifiers.
HOTKEY_VK = 0x23             # VK_END
# (Ctrl+Alt = toggle, Ctrl+Alt+Shift = quit)
# ------------------------------------------------------

# ---- Virtual-key codes ----
VK_CONTROL = 0x11
VK_MENU = 0x12     # ALT
VK_SHIFT = 0x10

# ---- Win32 hook constants ----
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
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

LLKHF_EXTENDED = 0x01
LLKHF_UP = 0x80
LLMHF_INJECTED = 0x01

SM_CXSCREEN = 0
SM_CYSCREEN = 1

WHEEL_DELTA = 120

LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t

user32 = ctypes.windll.user32
HOOKPROC = ctypes.CFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", POINT),
                ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


def key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


# ============================================================
# Shared state + UDP sender
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

    def keyboard(self, keys):
        with self.lock:
            self.sock.sendto(P.pack_keyboard(self._next(P.T_KEYBOARD), keys), self.addr)

    def control(self, subtype):
        with self.lock:
            self.sock.sendto(P.pack_control(self._next(P.T_CONTROL), subtype), self.addr)


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.capture = False
        self.pressed_keys = set()   # u16: (extended<<8)|scancode
        self.mouse_buttons = 0


# ============================================================
# Keyboard + mouse hooks (run on their own thread w/ message loop)
# ============================================================
class HookThread(threading.Thread):
    def __init__(self, sender, state, on_quit):
        super().__init__(daemon=True)
        self.sender = sender
        self.state = state
        self.on_quit = on_quit
        self.thread_id = None
        self._kb_proc = HOOKPROC(self._keyboard_proc)
        self._ms_proc = HOOKPROC(self._mouse_proc)
        self._kb_hook = None
        self._ms_hook = None
        self.cx = user32.GetSystemMetrics(SM_CXSCREEN) // 2
        self.cy = user32.GetSystemMetrics(SM_CYSCREEN) // 2

    # ---- capture toggle ----
    def _set_capture(self, on):
        with self.state.lock:
            self.state.capture = on
            self.state.pressed_keys.clear()
            self.state.mouse_buttons = 0
        if on:
            user32.SetCursorPos(self.cx, self.cy)
            print("\n[CAPTURE ON] keyboard+mouse now drive the gaming PC "
                  "(Ctrl+Alt+End to release).")
            self._beep(True)
        else:
            # Tell the receiver to drop everything immediately.
            self.sender.control(P.CTRL_RELEASE_ALL_KBM)
            print("\n[CAPTURE OFF] Surface keyboard+mouse restored.")
            self._beep(False)

    @staticmethod
    def _beep(on):
        if winsound:
            winsound.Beep(1200 if on else 600, 80)

    # ---- keyboard hook ----
    def _keyboard_proc(self, nCode, wParam, lParam):
        if nCode == 0:
            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            is_up = wParam in (WM_KEYUP, WM_SYSKEYUP)
            is_down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)

            # Reserved hotkeys: act on key-down, swallow both down and up.
            if kb.vkCode == HOTKEY_VK and key_down(VK_CONTROL) and key_down(VK_MENU):
                if is_down:
                    if key_down(VK_SHIFT):
                        self._request_quit()
                    else:
                        with self.state.lock:
                            now_on = not self.state.capture
                        self._set_capture(now_on)
                return 1  # swallow

            with self.state.lock:
                capturing = self.state.capture
            if capturing:
                extended = 1 if (kb.flags & LLKHF_EXTENDED) else 0
                key = (extended << 8) | (kb.scanCode & 0xFF)
                with self.state.lock:
                    if is_down:
                        self.state.pressed_keys.add(key)
                    elif is_up:
                        self.state.pressed_keys.discard(key)
                    snapshot = list(self.state.pressed_keys)
                self.sender.keyboard(snapshot)
                return 1  # swallow locally (full takeover)

        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    # ---- mouse hook ----
    def _mouse_proc(self, nCode, wParam, lParam):
        if nCode == 0:
            with self.state.lock:
                capturing = self.state.capture
            if capturing:
                ms = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                injected = bool(ms.flags & LLMHF_INJECTED)

                if wParam == WM_MOUSEMOVE:
                    if not injected:
                        dx = ms.pt.x - self.cx
                        dy = ms.pt.y - self.cy
                        if dx or dy:
                            self.sender.mouse(dx, dy, 0, self.state.mouse_buttons)
                        user32.SetCursorPos(self.cx, self.cy)  # re-pin to center
                    return 1

                if wParam == WM_MOUSEWHEEL:
                    delta = ctypes.c_short((ms.mouseData >> 16) & 0xFFFF).value
                    self.sender.mouse(0, 0, delta // WHEEL_DELTA, self.state.mouse_buttons)
                    return 1

                btn = self._button_for(wParam, ms.mouseData)
                if btn is not None:
                    down = wParam in (WM_LBUTTONDOWN, WM_RBUTTONDOWN,
                                      WM_MBUTTONDOWN, WM_XBUTTONDOWN)
                    with self.state.lock:
                        if down:
                            self.state.mouse_buttons |= btn
                        else:
                            self.state.mouse_buttons &= ~btn
                        buttons = self.state.mouse_buttons
                    self.sender.mouse(0, 0, 0, buttons)
                    return 1

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

    def _request_quit(self):
        if self.thread_id is not None:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)

    # ---- thread body ----
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

        # WM_QUIT received -> clean up and signal main.
        if self._kb_hook:
            user32.UnhookWindowsHookEx(self._kb_hook)
        if self._ms_hook:
            user32.UnhookWindowsHookEx(self._ms_hook)
        self.on_quit()


# ============================================================
# Keepalive: resend held kbd/mouse state so the receiver watchdog stays alive
# ============================================================
def keepalive_loop(sender, state, stop):
    interval = KEEPALIVE_MS / 1000.0
    while not stop.is_set():
        with state.lock:
            if state.capture:
                keys = list(state.pressed_keys)
                buttons = state.mouse_buttons
                active = True
            else:
                active = False
        if active:
            sender.keyboard(keys)
            sender.mouse(0, 0, 0, buttons)
        time.sleep(interval)


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
              "(keyboard/mouse still work). pip install XInput-Python to enable.")
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
    state = State()
    stop = threading.Event()

    def on_quit():
        stop.set()

    hooks = HookThread(sender, state, on_quit)
    ctrl = threading.Thread(target=controller_loop, args=(sender, stop), daemon=True)
    keep = threading.Thread(target=keepalive_loop, args=(sender, state, stop), daemon=True)

    print(f"InputRelay sender -> {TARGET_IP}:{TARGET_PORT}")
    print("Controller relay: ON (always).")
    print("Keyboard/mouse capture: OFF. Press Ctrl+Alt+End to toggle, "
          "Ctrl+Alt+Shift+End to quit.")

    hooks.start()
    ctrl.start()
    keep.start()

    try:
        while not stop.is_set():
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        # If we still have capture on, make sure the receiver releases everything.
        sender.control(P.CTRL_RELEASE_ALL_KBM)
        hooks._request_quit()
        time.sleep(0.2)
        print("\nBye.")


if __name__ == "__main__":
    main()
