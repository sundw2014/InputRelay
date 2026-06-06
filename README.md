# InputRelay

A software KVM: relay a **game controller, keyboard, and mouse** from one Windows
PC (e.g. a Surface Book) to another (your gaming PC) over WiFi/UDP.

```
                Surface Book (relay_sender.py)            Gaming PC (receiver.py)
 controller --BT--> XInput ─────────► gamepad packets ─┐
 keyboard ───────► LL hook (capture) ► key snapshots ──┼─UDP─►  ├─ virtual Xbox 360 pad (ViGEmBus)
 mouse ──────────► LL hook (capture) ► mouse deltas ───┤        ├─ keyboard via SendInput
                                                       ┘        └─ mouse via SendInput
```

- The **controller** is always relayed and behaves on the gaming PC as if plugged
  in there.
- The **keyboard + mouse** are relayed only in **capture mode**, which you toggle
  with a hotkey. In capture mode the Surface keyboard/mouse drive the gaming PC
  and are blocked locally (full takeover). Toggle it off to use the Surface again.

## Files

| File | Runs on | Purpose |
|------|---------|---------|
| `relay_sender.py` | Surface Book (Windows) | capture controller + kbd + mouse, send over UDP |
| `receiver.py` | Gaming PC (Windows) | replay onto a virtual gamepad + SendInput kbd/mouse |
| `protocol.py` | both | shared UDP packet format (imported by the two above) |
| `requirements-sender.txt` / `requirements-receiver.txt` | resp. PC | Python deps |

Both ends must have `protocol.py` next to the script they run.

## Hotkeys (on the Surface, configurable in `relay_sender.py`)

| Hotkey | Action |
|--------|--------|
| **Ctrl + Alt + End** | toggle keyboard/mouse capture on/off |
| **Ctrl + Alt + Shift + End** | quit the program (always restores local input) |

A short beep confirms each toggle (rising = capture on, falling = off). If the
program ever exits or crashes, Windows removes the hooks automatically, so your
Surface input is never permanently locked.

---

## Setup

### Gaming PC (receiver)
1. Install the **ViGEmBus** driver: <https://github.com/nefarius/ViGEmBus/releases>
2. `pip install -r requirements-receiver.txt`
3. `python receiver.py`  (keep `protocol.py` in the same folder)
4. Allow `python.exe` through the firewall on **Private** networks if prompted,
   or UDP packets get dropped. Note this PC's LAN IP (`ipconfig`).

### Surface Book (sender)
1. Pair the Xbox controller via **Settings > Bluetooth & devices > Add device**
   (it appears as a normal XInput gamepad — no driver needed).
2. `pip install -r requirements-sender.txt`
3. Edit **`TARGET_IP`** near the top of `relay_sender.py` to the gaming PC's IP.
4. `python relay_sender.py`  (keep `protocol.py` in the same folder)
   - Low-level hooks may require running the terminal **as Administrator** (and
     they can't capture input over apps that are themselves elevated/UAC).

### Try it
- Controller: works on the gaming PC immediately (test with `joy.cpl` / Steam).
- Keyboard + mouse: press **Ctrl+Alt+End** on the Surface — now your typing and
  mouse movement go to the gaming PC. Press it again to get the Surface back.

---

## How it stays reliable over UDP

- **Stateful inputs** (gamepad, held keys, mouse buttons) are sent as full-state
  snapshots plus a 100 ms keepalive. A dropped packet self-corrects on the next
  snapshot, and per-class **watchdogs** on the receiver release everything if a
  stream stops — so a key/button never sticks. Leaving capture mode also sends an
  explicit `RELEASE_ALL` so the gaming PC drops keys/buttons instantly.
- **Mouse motion/wheel** are deltas: sent only when non-zero and never in a
  keepalive (re-sending a delta would double-move the cursor). Mouse capture
  re-pins the cursor to screen center each move (the standard FPS technique) to
  get true relative motion with no screen-edge clamping.

## Limitations

- **Windows only** on both ends (Win32 hooks, SendInput, ViGEmBus, XInput).
- Games that read input via **Raw Input** (many competitive FPS) may ignore the
  synthetic `SendInput` keyboard/mouse. Upgrade path: the
  [Interception](https://github.com/oblitum/Interception) driver on the gaming
  PC. The controller path is unaffected (ViGEmBus is a real virtual device).
- Keep the controller paired **only** to the Surface; otherwise the gaming PC
  would just use it directly.
- Injecting synthetic input can be flagged by anti-cheat in online games — fine
  for single-player, your own tools, and remote-control use.

## Protocol

UDP, little-endian. Two families share the socket:
- **Legacy** (`magic 0xA5`, 15 B): the old ESP32/gamepad-only packet, still
  accepted by the receiver.
- **Typed** (`magic 0xA6`): `header(magic,type,seq)` + per-type payload for
  gamepad / mouse / keyboard / control. See `protocol.py` for exact layouts.
