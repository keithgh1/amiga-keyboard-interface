# Amiga 500 control

To read the Amiga's screen or send it keyboard/mouse input, follow [USAGE.md](USAGE.md).
Quick reference:
- Screen: `curl -s -o captures/now.jpg "$AMIGA_SNAPSHOT_URL"`, then read the JPEG.
- Keys: `python tools/amikbd.py 'T dir\n' 'WAITDISK'` (single quotes so `\n` reaches the board).
- Mouse: `python tools/pointer.py --goto X Y` then `python tools/amikbd.py "CLICK L 2"`.
- Status: `python tools/amikbd.py S` (needs `sync=1`; `kclk=0 kdat=0` means the Amiga is off).
- Compile + run C on the A500: `python tools/amirun.py prog.c` (vbcc `+a500`; needs LinkAgent running: `disk/LinkAgent.adf`).
  Files only: `python tools/amipush.py put local RAM:name`. See [devchain/README.md](devchain/README.md).

Command reference: [firmware/amikbd/PROTOCOL.md](firmware/amikbd/PROTOCOL.md).

Habits that keep the loop reliable:
- Look, act, wait, look. After anything that loads from disk, use `WAITDISK` before the next snapshot.
- Take click targets from a fresh snapshot, and don't position the pointer while it shows the "zzz" busy pointer.
- Keep LinkAgent's window strip visible; transfers read their acknowledgements from it.
