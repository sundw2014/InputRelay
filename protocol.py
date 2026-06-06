"""
protocol.py -- shared wire format for InputRelay.

Both the sender (Surface) and the receiver (gaming PC) import this so the packet
layout and constants can never drift apart.

Transport: UDP. Two packet families share the socket:

  * Legacy gamepad packet (magic 0xA5, 15 bytes) -- what the old ESP32 firmware
    and surface_sender.py emit. Still accepted by the receiver for compatibility.

  * Typed packet (magic 0xA6): header + per-type payload, used for the
    multi-device relay (gamepad / mouse / keyboard / control).

Robustness model (important):
  * Stateful inputs (gamepad, mouse buttons, held keys) are sent as FULL-STATE
    snapshots plus a periodic keepalive. A dropped UDP packet self-corrects on
    the next snapshot, and the receiver's watchdog releases everything if the
    stream stops -- so nothing ever sticks.
  * Mouse MOTION/WHEEL are deltas: sent only when non-zero, never in a keepalive
    (re-sending a delta would double-move the cursor).
"""

import struct

# ---------------- magic bytes ----------------
MAGIC_LEGACY = 0xA5   # old 15-byte gamepad-only packet
MAGIC_TYPED  = 0xA6   # new typed multi-device packet

# ---------------- packet types (typed family) ----------------
T_GAMEPAD  = 0
T_MOUSE    = 1
T_KEYBOARD = 2
T_CONTROL  = 3

# ---------------- control subtypes ----------------
CTRL_RELEASE_ALL_KBM = 0   # release all keyboard keys + mouse buttons now

# ---------------- canonical gamepad button bits ----------------
BTN_A     = 1 << 0
BTN_B     = 1 << 1
BTN_X     = 1 << 2
BTN_Y     = 1 << 3
BTN_LB    = 1 << 4
BTN_RB    = 1 << 5
BTN_VIEW  = 1 << 6
BTN_MENU  = 1 << 7
BTN_LS    = 1 << 8
BTN_RS    = 1 << 9
BTN_GUIDE = 1 << 10

# ---------------- d-pad bits ----------------
DP_UP    = 1 << 0
DP_DOWN  = 1 << 1
DP_LEFT  = 1 << 2
DP_RIGHT = 1 << 3

# ---------------- mouse button bits ----------------
MB_LEFT   = 1 << 0
MB_RIGHT  = 1 << 1
MB_MIDDLE = 1 << 2
MB_X1     = 1 << 3
MB_X2     = 1 << 4

# ---------------- struct formats ----------------
# Header for typed packets: magic, type, seq
_HEADER_FMT = "<BBB"
HEADER_SIZE = struct.calcsize(_HEADER_FMT)  # 3

# Gamepad payload (also the body of the legacy packet after magic+seq):
#   buttons, dpad, lx, ly, rx, ry, lt, rt
_GAMEPAD_FMT = "<HBhhhhBB"
GAMEPAD_PAYLOAD_SIZE = struct.calcsize(_GAMEPAD_FMT)  # 13

# Legacy packet: magic, seq, <gamepad payload>
_LEGACY_FMT = "<BB" + _GAMEPAD_FMT[1:]
LEGACY_SIZE = struct.calcsize(_LEGACY_FMT)  # 15

# Mouse payload: dx, dy, wheel, buttons
_MOUSE_FMT = "<hhbB"
MOUSE_PAYLOAD_SIZE = struct.calcsize(_MOUSE_FMT)  # 6

# Keyboard payload is variable length: count(B) followed by count * u16 keys.
# Each key = (extended << 8) | scancode.

# Control payload: subtype
_CONTROL_FMT = "<B"


# ===================== packing (sender) =====================

def pack_gamepad(seq, buttons, dpad, lx, ly, rx, ry, lt, rt):
    return (struct.pack(_HEADER_FMT, MAGIC_TYPED, T_GAMEPAD, seq & 0xFF) +
            struct.pack(_GAMEPAD_FMT, buttons, dpad, lx, ly, rx, ry, lt, rt))


def pack_mouse(seq, dx, dy, wheel, buttons):
    return (struct.pack(_HEADER_FMT, MAGIC_TYPED, T_MOUSE, seq & 0xFF) +
            struct.pack(_MOUSE_FMT, dx, dy, wheel, buttons))


def pack_keyboard(seq, keys):
    """keys: iterable of u16 values, each (extended << 8) | scancode."""
    keys = list(keys)[:255]
    body = struct.pack("<B", len(keys)) + struct.pack("<%dH" % len(keys), *keys)
    return struct.pack(_HEADER_FMT, MAGIC_TYPED, T_KEYBOARD, seq & 0xFF) + body


def pack_control(seq, subtype):
    return (struct.pack(_HEADER_FMT, MAGIC_TYPED, T_CONTROL, seq & 0xFF) +
            struct.pack(_CONTROL_FMT, subtype))


# ===================== parsing (receiver) =====================

class Packet:
    __slots__ = ("magic", "ptype", "seq", "fields")

    def __init__(self, magic, ptype, seq, fields):
        self.magic = magic
        self.ptype = ptype
        self.seq = seq
        self.fields = fields  # dict of type-specific fields


def parse(data):
    """Parse a datagram into a Packet, or return None if malformed/unknown."""
    if len(data) < 2:
        return None
    magic = data[0]

    # Legacy gamepad packet: magic 0xA5, fixed 15 bytes.
    if magic == MAGIC_LEGACY:
        if len(data) != LEGACY_SIZE:
            return None
        _, seq, buttons, dpad, lx, ly, rx, ry, lt, rt = struct.unpack(_LEGACY_FMT, data)
        return Packet(magic, T_GAMEPAD, seq, {
            "buttons": buttons, "dpad": dpad,
            "lx": lx, "ly": ly, "rx": rx, "ry": ry, "lt": lt, "rt": rt,
        })

    if magic != MAGIC_TYPED or len(data) < HEADER_SIZE:
        return None
    _, ptype, seq = struct.unpack(_HEADER_FMT, data[:HEADER_SIZE])
    body = data[HEADER_SIZE:]

    if ptype == T_GAMEPAD:
        if len(body) != GAMEPAD_PAYLOAD_SIZE:
            return None
        buttons, dpad, lx, ly, rx, ry, lt, rt = struct.unpack(_GAMEPAD_FMT, body)
        return Packet(magic, ptype, seq, {
            "buttons": buttons, "dpad": dpad,
            "lx": lx, "ly": ly, "rx": rx, "ry": ry, "lt": lt, "rt": rt,
        })

    if ptype == T_MOUSE:
        if len(body) != MOUSE_PAYLOAD_SIZE:
            return None
        dx, dy, wheel, buttons = struct.unpack(_MOUSE_FMT, body)
        return Packet(magic, ptype, seq, {
            "dx": dx, "dy": dy, "wheel": wheel, "buttons": buttons,
        })

    if ptype == T_KEYBOARD:
        if len(body) < 1:
            return None
        count = body[0]
        expected = 1 + count * 2
        if len(body) != expected:
            return None
        keys = struct.unpack("<%dH" % count, body[1:]) if count else ()
        return Packet(magic, ptype, seq, {"keys": list(keys)})

    if ptype == T_CONTROL:
        if len(body) != struct.calcsize(_CONTROL_FMT):
            return None
        (subtype,) = struct.unpack(_CONTROL_FMT, body)
        return Packet(magic, ptype, seq, {"subtype": subtype})

    return None


def seq_is_newer(new, last):
    """True if 8-bit `new` is newer than `last`, tolerating wraparound."""
    return ((new - last) & 0xFF) < 128
