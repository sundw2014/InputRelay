#!/usr/bin/env python3
"""
receiver.py -- gaming-PC side of InputRelay (run this on the machine you control).

Receives UDP packets from the Surface relay and replays them locally:
  * gamepad  -> a virtual Xbox 360 controller (vgamepad / ViGEmBus)
  * keyboard -> SendInput scancode key presses
  * mouse    -> SendInput relative move / wheel / buttons

Also accepts the legacy 15-byte gamepad packet from the old ESP32 firmware.

Setup (once, Windows):
    1. Install the ViGEmBus driver:  https://github.com/nefarius/ViGEmBus/releases
    2. pip install vgamepad
    3. python receiver.py

Safety: per-class watchdogs release everything if a stream stops, and an explicit
RELEASE_ALL control packet (sent when the Surface leaves capture mode) drops all
keys/buttons immediately -- so nothing ever sticks on this PC.
"""

import ctypes
import socket
import sys
import time
from ctypes import wintypes

import protocol as P

try:
    import vgamepad as vg
except ImportError:
    sys.exit(
        "vgamepad is not installed.\n"
        "  1. Install the ViGEmBus driver: https://github.com/nefarius/ViGEmBus/releases\n"
        "  2. pip install vgamepad\n"
    )

# ----------------------- config -----------------------
LISTEN_IP = "0.0.0.0"
LISTEN_PORT = 9999
GAMEPAD_TIMEOUT = 0.25   # s without gamepad data -> neutralize sticks/buttons
KBM_TIMEOUT = 0.30       # s without kbd/mouse data -> release all keys/buttons
# ------------------------------------------------------


# ============================================================
# Windows SendInput plumbing (pure ctypes)
# ============================================================
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
XBUTTON1 = 0x0001
XBUTTON2 = 0x0002
WHEEL_DELTA = 120

ULONG_PTR = ctypes.POINTER(wintypes.ULONG)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]


_SendInput = ctypes.windll.user32.SendInput


def _send(*inputs):
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    _SendInput(n, arr, ctypes.sizeof(INPUT))


def _kbd_input(scancode, extended, keyup):
    flags = KEYEVENTF_SCANCODE
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    if keyup:
        flags |= KEYEVENTF_KEYUP
    return INPUT(type=INPUT_KEYBOARD,
                 u=_INPUTunion(ki=KEYBDINPUT(wVk=0, wScan=scancode,
                                             dwFlags=flags, time=0, dwExtraInfo=None)))


def _mouse_input(dx=0, dy=0, mouse_data=0, flags=0):
    return INPUT(type=INPUT_MOUSE,
                 u=_INPUTunion(mi=MOUSEINPUT(dx=dx, dy=dy, mouseData=mouse_data,
                                             dwFlags=flags, time=0, dwExtraInfo=None)))


# ============================================================
# Injectors
# ============================================================
class KeyboardInjector:
    """Applies full-state keyboard snapshots by diffing against current state."""

    def __init__(self):
        self.pressed = set()  # set of u16 (extended<<8 | scancode)

    def apply_snapshot(self, keys):
        new = set(keys)
        for k in new - self.pressed:
            self._emit(k, keyup=False)
        for k in self.pressed - new:
            self._emit(k, keyup=True)
        self.pressed = new

    def release_all(self):
        for k in list(self.pressed):
            self._emit(k, keyup=True)
        self.pressed.clear()

    @staticmethod
    def _emit(key, keyup):
        scancode = key & 0xFF
        extended = (key >> 8) & 0x1
        _send(_kbd_input(scancode, extended, keyup))


class MouseInjector:
    """Relative motion + wheel (deltas) and buttons (full-state diff)."""

    _BTN_FLAGS = [
        (P.MB_LEFT,   MOUSEEVENTF_LEFTDOWN,   MOUSEEVENTF_LEFTUP,   0),
        (P.MB_RIGHT,  MOUSEEVENTF_RIGHTDOWN,  MOUSEEVENTF_RIGHTUP,  0),
        (P.MB_MIDDLE, MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, 0),
        (P.MB_X1,     MOUSEEVENTF_XDOWN,      MOUSEEVENTF_XUP,      XBUTTON1),
        (P.MB_X2,     MOUSEEVENTF_XDOWN,      MOUSEEVENTF_XUP,      XBUTTON2),
    ]

    def __init__(self):
        self.buttons = 0

    def apply(self, dx, dy, wheel, buttons):
        if dx or dy:
            _send(_mouse_input(dx=dx, dy=dy, flags=MOUSEEVENTF_MOVE))
        if wheel:
            _send(_mouse_input(mouse_data=wheel * WHEEL_DELTA, flags=MOUSEEVENTF_WHEEL))
        if buttons != self.buttons:
            self._apply_buttons(buttons)

    def _apply_buttons(self, buttons):
        for bit, down, up, xdata in self._BTN_FLAGS:
            was = self.buttons & bit
            now = buttons & bit
            if now and not was:
                _send(_mouse_input(mouse_data=xdata, flags=down))
            elif was and not now:
                _send(_mouse_input(mouse_data=xdata, flags=up))
        self.buttons = buttons

    def release_all(self):
        if self.buttons:
            self._apply_buttons(0)


# Map canonical gamepad bits -> XUSB flags.
XUSB = vg.XUSB_BUTTON
_BUTTON_MAP = [
    (P.BTN_A,     XUSB.XUSB_GAMEPAD_A),
    (P.BTN_B,     XUSB.XUSB_GAMEPAD_B),
    (P.BTN_X,     XUSB.XUSB_GAMEPAD_X),
    (P.BTN_Y,     XUSB.XUSB_GAMEPAD_Y),
    (P.BTN_LB,    XUSB.XUSB_GAMEPAD_LEFT_SHOULDER),
    (P.BTN_RB,    XUSB.XUSB_GAMEPAD_RIGHT_SHOULDER),
    (P.BTN_VIEW,  XUSB.XUSB_GAMEPAD_BACK),
    (P.BTN_MENU,  XUSB.XUSB_GAMEPAD_START),
    (P.BTN_LS,    XUSB.XUSB_GAMEPAD_LEFT_THUMB),
    (P.BTN_RS,    XUSB.XUSB_GAMEPAD_RIGHT_THUMB),
    (P.BTN_GUIDE, XUSB.XUSB_GAMEPAD_GUIDE),
]
_DPAD_MAP = [
    (P.DP_UP,    XUSB.XUSB_GAMEPAD_DPAD_UP),
    (P.DP_DOWN,  XUSB.XUSB_GAMEPAD_DPAD_DOWN),
    (P.DP_LEFT,  XUSB.XUSB_GAMEPAD_DPAD_LEFT),
    (P.DP_RIGHT, XUSB.XUSB_GAMEPAD_DPAD_RIGHT),
]


class GamepadInjector:
    def __init__(self):
        self.pad = vg.VX360Gamepad()
        self.neutralize()

    def apply(self, f):
        w = 0
        for bit, flag in _BUTTON_MAP:
            if f["buttons"] & bit:
                w |= flag
        for bit, flag in _DPAD_MAP:
            if f["dpad"] & bit:
                w |= flag
        r = self.pad.report
        r.wButtons = w
        r.bLeftTrigger = f["lt"]
        r.bRightTrigger = f["rt"]
        r.sThumbLX = f["lx"]
        r.sThumbLY = f["ly"]
        r.sThumbRX = f["rx"]
        r.sThumbRY = f["ry"]
        self.pad.update()

    def neutralize(self):
        self.pad.reset()
        self.pad.update()


# ============================================================
# Main loop
# ============================================================
def main():
    gamepad = GamepadInjector()
    keyboard = KeyboardInjector()
    mouse = MouseInjector()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((LISTEN_IP, LISTEN_PORT))
    sock.settimeout(0.05)

    print(f"InputRelay receiver listening on UDP {LISTEN_IP}:{LISTEN_PORT}")
    print("Virtual Xbox 360 controller created. Ctrl+C to quit.")

    last_gp_seq = None
    last_kb_seq = None
    gp_seen = 0.0
    kbm_seen = 0.0
    gp_neutral = True
    kbm_neutral = True

    while True:
        try:
            data, _addr = sock.recvfrom(512)
        except socket.timeout:
            data = None

        now = time.time()

        if data:
            pkt = P.parse(data)
            if pkt is not None:
                if pkt.ptype == P.T_GAMEPAD:
                    if last_gp_seq is None or P.seq_is_newer(pkt.seq, last_gp_seq):
                        last_gp_seq = pkt.seq
                        gamepad.apply(pkt.fields)
                        gp_seen = now
                        gp_neutral = False
                elif pkt.ptype == P.T_KEYBOARD:
                    if last_kb_seq is None or P.seq_is_newer(pkt.seq, last_kb_seq):
                        last_kb_seq = pkt.seq
                        keyboard.apply_snapshot(pkt.fields["keys"])
                        kbm_seen = now
                        kbm_neutral = False
                elif pkt.ptype == P.T_MOUSE:
                    # Deltas: do NOT seq-drop, every packet carries unique motion.
                    f = pkt.fields
                    mouse.apply(f["dx"], f["dy"], f["wheel"], f["buttons"])
                    kbm_seen = now
                    kbm_neutral = False
                elif pkt.ptype == P.T_CONTROL:
                    if pkt.fields["subtype"] == P.CTRL_RELEASE_ALL_KBM:
                        keyboard.release_all()
                        mouse.release_all()
                        kbm_neutral = True

        # Watchdogs.
        if not gp_neutral and (now - gp_seen) > GAMEPAD_TIMEOUT:
            gamepad.neutralize()
            gp_neutral = True
        if not kbm_neutral and (now - kbm_seen) > KBM_TIMEOUT:
            keyboard.release_all()
            mouse.release_all()
            kbm_neutral = True


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBye.")
