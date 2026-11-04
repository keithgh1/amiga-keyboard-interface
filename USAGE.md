# Driving the Amiga 500 from the PC

How to **read the Amiga's screen** and **send keyboard and mouse input** to it. Tested on a PAL A500 with
Kickstart and Workbench 1.3.

```
 PC ──USB──> Pro Micro (amikbd firmware) ──KCLK/KDAT/RESET──> A500 keyboard header CN13
 PC <──USB── HDMI capture device (tools/capture.py) <── Amiga video
```

Related: [firmware/amikbd/PROTOCOL.md](firmware/amikbd/PROTOCOL.md) (full command reference),
[hardware/WIRING.md](hardware/WIRING.md) (board wiring), [devchain/README.md](devchain/README.md)
(LinkAgent file transfer and the compile-send-run loop).

---

## 1. Power-on order
1. Real A500 keyboard unplugged from CN13; board cable on CN13 with the **red wire on pin 1**.
2. Plug the Pro Micro into USB.
3. Power on the Amiga. The board syncs with the Amiga by itself during boot.
4. Check: `python tools/amikbd.py S` should show `sync=1 kclk=1 kdat=1 rst=1`.

## 2. Requirements (PC side)
- Python 3 with `pyserial`, `Pillow` and `numpy`.
- No COM port setup: the tools find the board by USB ID 2341:8036. The COM number can change when the
  board is re-plugged.
- [ffmpeg](https://ffmpeg.org/) on PATH (`winget install Gyan.FFmpeg`), used by `tools/capture.py`.
- An HDMI capture device that Windows lists as a camera or capture device (tested with an EVGA XR1 Lite).
  Start `python tools/capture.py --device NAME` and leave it running; `--list` shows the names, and
  part of a name is enough. Close OBS or any other program using the device first.

---

## 3. Reading the screen
```bash
python tools/capture.py --snap captures/now.jpg
```
Then open the JPEG (Claude: `Read captures/now.jpg`). Save frames under `captures/`.

Every tool (amikbd `--snap`, pointer, amipush, amirun) gets its frames from the running `capture.py`.

- **"NO SIGNAL" in the frame** means the Amiga is off, or the video chain is down.
- Coordinates in this guide are for a 1280×720 capture, where the Workbench title bar is at about y=50.

### Finding the mouse pointer
```bash
python tools/pointer.py                 # prints pointer: (x, y) of the red arrow tip, capture pixels
python tools/pointer.py captures/x.jpg  # same, on a saved frame
```
It looks for the red Workbench 1.3 arrow. It **cannot see the "zzz" busy pointer**, so wait for the busy
pointer to clear before positioning.

---

## 4. Sending input
All input goes through `tools/amikbd.py`. Each argument is one firmware command. Each command waits for
`OK`/`ERR`, and the exit code is non-zero if any command failed.

```bash
python tools/amikbd.py "<cmd>" ["<cmd>" ...] [--snap captures/after.jpg] [--snap-delay 0.5]
python tools/amikbd.py -i      # interactive: type commands line by line
```
`--snap` grabs a screen frame after the commands. Lines starting with `#` are events from the board
(`# LED drive=1`, `# SYNC ok`, ...).

### Quoting
The firmware understands the escapes `\n` (Return), `\t` (Tab), `\e` (Esc), `\b` (Backspace) and `\\`.
The backslash must reach the board literally:
- **bash:** use single quotes: `python tools/amikbd.py 'T dir\n'`
- **PowerShell:** single quotes too: `python tools/amikbd.py 'T dir\n'`
- Never put the command inside `python -c "..."` with `\n`: it turns into a real newline and Return is
  never sent. (It reports `OK 3` instead of `OK 4` for `dir\n`.)

### Keyboard commands
| Command | Example | Notes |
|---|---|---|
| `T <text>` | `'T dir df0:\n'` | US layout; Shift added automatically. Reply `OK <keys>`. |
| `K <keys>` | `K RAMIGA+Q`, `K CTRL+C`, `K LAMIGA+M`, `K F1`, `K RETURN`, `K UP` | Press and release. |
| `D <keys>` / `U <keys>` | `D LSHIFT` … `U LSHIFT` | Hold / release. |
| `X` | | Release everything still held. |
| `R` | | Hardware reset (pulls /KBRESET). `K CTRL+LAMIGA+RAMIGA` does the same. |

Key names: `LAMIGA` (`LA`), `RAMIGA` (`RA`), `LSHIFT`, `RSHIFT`, `CTRL`, `LALT`, `RALT`, `CAPS`, `RETURN`,
`KPENTER`, `SPACE`, `BS`, `TAB`, `ESC`, `DEL`, `HELP`, `UP`/`DOWN`/`LEFT`/`RIGHT`, `F1`–`F10`, `KP0`–`KP9`,
`KPDOT`, `KPMINUS`, `KPPLUS`, `KPSTAR`, `KPSLASH`, `KPLPAREN`, `KPRPAREN`, any single character, or `$NN`.
Full table: [PROTOCOL.md](firmware/amikbd/PROTOCOL.md).

Useful AmigaOS keys:
- `K LAMIGA+M` / `K LAMIGA+N`: screen to back / Workbench to front.
- Right Amiga + letter: menu shortcuts in programs.
- `K CTRL+C`: break a running CLI command.
- `K UP`: Shell history (in shells that have it).

### Mouse commands (keyboard mouse: Left Amiga + cursor keys, Left Amiga + Alt = buttons)
**Preferred: closed-loop positioning from the video**
```bash
python tools/pointer.py --goto 650 262     # move the pointer tip to capture pixel (650, 262); ±3 px
python tools/amikbd.py "CLICK L 2"         # double-click
```
| Command | Notes |
|---|---|
| `CLICK [L\|R] [n]` | `n` = 1–3; `CLICK L 2` = double-click (opens icons). |
| `MDOWN [L\|R]` / `MUP [L\|R]` | Hold / release a button. Moves in between become drags. With R: hold, move to the menu item, release. |
| `MOVE <UP\|DOWN\|LEFT\|RIGHT> <n> [FAST]` | `n` taps; FAST adds Shift (much bigger steps). |
| `M <dx> <dy>` | Relative move in plain steps. |
| `HOME` | Pins the pointer at the top-left (Shift taps). |

Measured step sizes (1280×720 capture, WB 1.3); `pointer.py` uses these:

| Axis | Plain step | Shift step |
|---|---|---|
| X | 1.75 | ~178 |
| Y | 1.275 | ~94 |

Each step is a separate key press, so Intuition never accelerates the pointer. If your capture has a
different scale, adjust `STEP` in `tools/pointer.py`.

---

## 5. Knowing when the Amiga is done: the drive LED
The board reads the A500's power LED (A0, CN13 pin 7) and drive LED (A1, CN13 pin 8), even with the
real keyboard unplugged.

| Command | Use |
|---|---|
| `WAITDISK [quiet_ms] [timeout_ms]` | Blocks until the drive LED has been off for `quiet_ms` (default 1500) since the command started. `OK idle` / `ERR TIMEOUT`. |
| `L` | `OK power=1 drive=0 power_mv=2702 drive_mv=0` (~2.7 V = on) |
| `python tools/ledwatch.py --secs 15 'K RETURN'` | Run commands, then log every LED change with timestamps. |

Typical: `dir` + Return turns the drive LED on 0.1 s later, it stays on for ~8 s (load + motor run-on),
then goes off.

---

## 6. The basic loop (look, act, wait, look)
```bash
python tools/capture.py --snap captures/now.jpg                      # 1. look
python tools/pointer.py --goto 650 262                                # 2a. act: mouse
python tools/amikbd.py "CLICK L 2"
python tools/amikbd.py 'T dir\n' 'WAITDISK 1500 30000' --snap captures/after.jpg   # 2b. act: keys, wait, look
```

Tips:
- After opening anything from floppy, use `WAITDISK`, or wait a few seconds, before the next snapshot.
- Don't position the pointer while it shows "zzz".
- A double-click must land on the icon image, not the label. Take the target from a fresh snapshot.

### Choosing a disk on a Gotek
With FlashFloppy and its Amiga-side selector (AUTOBOOT.HFE), the selector runs from slot 0. The Gotek
remembers its last slot, so a reset normally boots slot 1 again. To reach the selector, press both
Gotek buttons together (nothing changes on screen), then reset: `python tools/amikbd.py R`.

Drive the selector with keys:
- `K HELP` (key list), `K UP`/`K DOWN`, `K RETURN` (open a folder, close help pages).
- `K RETURN` on an image opens the slots browser: move to a slot, then `K RETURN` sets it.
  The first line of each page jumps to the next page, so move off it before pressing RETURN.
- `K F9` saves the slots; `K F7` puts the highlighted image in slot 1 and reboots.
- The selector drops keys sent less than about a second apart. Send them one at a time and snapshot
  before F7.

While the Amiga runs, one Gotek button steps to the next or previous slot, which the Amiga sees as a
disk change. With the Workbench disk in slot 1 and `LinkAgent.adf` in slot 2, that is the one-drive
swap for `Run LinkAgent:LinkAgent`.

### Keymap
amikbd types US layout. If punctuation comes out wrong (for example `:` typed as `@`), the disk is
running a different keymap: type `T setmap usa1\n` first. The patched Workbench built by
`devchain/build.py` switches to `usa1` automatically.

---

## 7. Status and troubleshooting
`python tools/amikbd.py S` →
`OK sync= caps= kclk= kdat= rst= power= drive= power_mv= drive_mv= held= tx= ack= nak= resync= resets=`

| Symptom | Meaning / fix |
|---|---|
| `ERR NOSYNC` | The board isn't synced with the Amiga. If `kclk=0 kdat=0`, the Amiga is off (snapshot shows "NO SIGNAL"). When it powers on, `# SYNC ok` appears within a few seconds. `SYNC` forces a re-handshake. |
| `# SYNC lost` + `ERR NOACK` | The Amiga stopped acknowledging (powered off or crashed). The board falls back to power-up sync by itself. |
| `# RESYNC ok` | One key missed its handshake and was resent automatically. Harmless. |
| Typed text missing Return | Quoting: see section 4. |
| Pointer not found | Busy "zzz" pointer, or the pointer is off-screen. Wait, or use `HOME`. |
| Stuck modifier | `X` releases everything the board holds. |
| `amikbd: Pro Micro not found` | USB unplugged, or the board is in its bootloader. Re-plug it. |

---

## 8. Firmware
```bash
arduino-cli compile --upload -b arduino:avr:leonardo -p COM5 firmware/amikbd   # build + flash
arduino-cli compile -b arduino:avr:leonardo firmware/amikbd                    # compile only
```
Or open `firmware/amikbd/amikbd.ino` in the Arduino IDE with the board set to Arduino Leonardo.
Developed with arduino-cli 1.5.1 and the `arduino:avr` 1.8.8 core.
