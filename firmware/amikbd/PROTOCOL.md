# amikbd serial protocol

USB CDC (Pro Micro, VID 2341 PID 8036, any baud). One command per line (`\n` or `\r`).
Each command gets exactly one reply line beginning with `OK` or `ERR`.
Lines beginning with `# ` are asynchronous events and can arrive at any time.

Host helper: `python tools/amikbd.py "<cmd>" ["<cmd>" ...] [--snap file.jpg]`.

## Commands
| Command | Effect | Reply |
|---|---|---|
| `T <text>` | Type text, US layout. Escapes: `\n` Return, `\t` Tab, `\e` Esc, `\b` Backspace, `\\` backslash. The whole line is validated before anything is sent. | `OK <n>` / `ERR BADCHAR <i>` / `ERR NOACK at <i>` / `ERR NOSYNC` |
| `K <chord>` | Tap: press keys left to right, hold, release right to left. | `OK` / `OK RESET` / `ERR ...` |
| `D <chord>` | Press and hold. | |
| `U <chord>` | Release. | |
| `X` | Release every held key. | |
| `R [ms]` | Reset the Amiga: /KBRESET and KCLK low for `ms` (default 500, min 100). | `OK` |
| `SYNC` | Drop sync; redo the power-up handshake in the background. | `OK` |
| `WAITDISK [quiet_ms] [timeout_ms]` | Wait until the drive LED has been off for `quiet_ms` (default 1500) since the command started. Timeout defaults to 60000. | `OK idle` / `ERR TIMEOUT` |
| `RAW <hex>` | Send one 8-bit code (key \| 0x80 for up, or a special $F9–$FE). Rotation is applied by the firmware. | |
| `RAWHEX <hexbytes>` | v0.3: send 1–96 raw bytes (2–192 hex digits, no spaces). Validate the entire argument before sending. For LinkAgent file transfer. | `OK` / `ERR BADARG` / `ERR NOSYNC` / `ERR NOACK` |
| `S` | Status: sync, caps, line levels, LEDs, held keys, counters. | `OK sync=...` |
| `L` | LED state. | `OK power= drive= power_mv= drive_mv=` |
| `V` | Firmware version. | |
| `CFG [GAP\|HOLD\|RESET\|ATTN\|RAWBIT\|RAWATTN <n>]` | Show or set: gap between key events (ms, default 15), tap hold (ms, 25), reset pulse (ms, 500), attention pre-pulse (0/1, 1). `RAWBIT` (3..60 µs, default 60) and `RAWATTN` (0/1, default 1) set bit time and attention pulse for `RAWHEX` only; key commands always use 60 µs. Settings are lost on board reset. | `OK gap= hold= reset= attn= rawbit= rawattn= ...` |
| `?` | Help. | |

## Chords and key names

`RAWHEX` reports keyboard handshakes only. File transfer requires LinkAgent's
screen acknowledgement and final size/CRC32 verification; use `tools/amipush.py`.
See [the receiver guide](../../devchain/README.md) for setup and the bootable Gotek image.
A chord is keys joined with `+`, e.g. `LAMIGA+M`, `RAMIGA+Q`, `CTRL+C`, `LAMIGA+LALT`.
Each key is a name, a single character (its unshifted key, so `K LAMIGA+n` = `K LAMIGA+N`), or `$NN` raw code.

| Name(s) | Code | Name(s) | Code |
|---|---|---|---|
| `LAMIGA`, `LA` | $66 | `RAMIGA`, `RA` | $67 |
| `LSHIFT`, `SHIFT` | $60 | `RSHIFT` | $61 |
| `CTRL`, `CONTROL` | $63 | `CAPS`, `CAPSLOCK` | $62 |
| `LALT`, `ALT` | $64 | `RALT` | $65 |
| `RETURN`, `RET`, `ENTER` | $44 | `KPENTER` | $43 |
| `SPACE` | $40 | `BS`, `BACKSPACE` | $41 |
| `TAB` | $42 | `ESC` | $45 |
| `DEL` | $46 | `HELP` | $5F |
| `UP` `DOWN` `RIGHT` `LEFT` | $4C–$4F | `F1`–`F10` | $50–$59 |
| `KP0`–`KP9` | keypad | `KPDOT` `KPMINUS` `KPPLUS` `KPSTAR` `KPSLASH` `KPLPAREN` `KPRPAREN` | keypad |

`+` cannot appear inside a chord; use `SHIFT+=`.

## Special behaviour
- **Ctrl + Left Amiga + Right Amiga** held together triggers a hardware reset (like the real A500 keyboard)
  instead of sending the third key. A `K CTRL+LAMIGA+RAMIGA` therefore replies `OK RESET`.
- **Caps Lock** sends only on press; the up/down bit carries the new LED state. The firmware tracks it.
- **Sync:** after power-up or reset, the firmware clocks a `1` every 143 ms until the Amiga handshakes,
  then sends `$FD $FE`. Event `# SYNC ok clocks=N`. Commands that send keys reply `ERR NOSYNC` until then.
  It only clocks while KCLK and KDAT read high, so it stays quiet while the Amiga is off.
- **Missed handshake:** clocks 1s until handshake, sends `$F9` and retransmits (`# RESYNC ok`). If that fails
  → `# SYNC lost`, and background sync takes over.
- **LED events:** `# LED power=0|1`, `# LED drive=0|1` (thresholds 1.2 V on / 0.8 V off, 30 ms debounce).
  A0 = CN13 pin 7 = power LED, A1 = CN13 pin 8 = drive LED (confirmed 2026-10-20).

## Keyboard mouse
Intuition moves the pointer when an Amiga key is held with a cursor key: one step per key-down, larger steps
with Shift, and acceleration if the cursor key is held. Amiga + Left Alt = left button, Amiga + Right Alt =
right button. (Amiga Libraries manual, "Keyboard control of the pointer".) The firmware uses **Left Amiga**,
and builds moves from separate taps so there is no acceleration.

| Command | Effect |
|---|---|
| `M <dx> <dy>` | Relative move in plain steps (+x right, +y down). With `MFAST` set, uses Shift steps for the bulk. |
| `MOVE <UP\|DOWN\|LEFT\|RIGHT> <n> [FAST]` | `n` taps in one direction, optionally with Shift. |
| `HOME` | `MHOME` Shift taps left, then up, so the pointer is pinned at the top-left corner. |
| `MABS <x> <y>` | `HOME`, then `M x y`. Coordinates in plain steps from the top-left. |
| `CLICK [L\|R] [count]` | Click (count 1–3; 2 = double-click). Default L. |
| `MDOWN [L\|R]` / `MUP [L\|R]` | Hold / release a button. Moves made while it is held become drags, or menu browsing for R. |
| `CFG MFAST <n>` | Plain steps covered by one Shift step (0 = don't use Shift; default 0 until calibrated). |
| `CFG MHOME <n>` | Shift taps per axis for `HOME` (default 40). |

Left Amiga is held through the whole move and released afterwards. If the host already holds it (`D LAMIGA`)
or a button is down, it stays held. Step sizes in pixels still need calibrating against the video.
