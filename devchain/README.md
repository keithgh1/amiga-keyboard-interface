# LinkAgent: PC to Amiga 500 file transfer

LinkAgent is a CLI program for a 68000 Amiga running Kickstart/Workbench 1.3.
It receives arbitrary binary files through the existing Pro Micro keyboard cable.
The PC reads acknowledgements from a barcode in the Amiga window via the existing
video capture (`AMIGA_SNAPSHOT_URL`). Keep that window visible during transfers.

## First use

1. Copy `build/LinkAgent-WB13.adf` onto the Gotek USB stick as another disk image.
   Build it from your own Workbench 1.3 ADF (see Build and validation); the receiver is added to C:.
   The original image was left unchanged. Select the new image and boot the A500.
   The startup script copies the receiver to RAM: and launches its own window.
2. Upload the updated keyboard firmware from a PowerShell in the project folder:

   ```powershell
   arduino-cli compile --upload -b arduino:avr:leonardo -p COM5 firmware/amikbd
   python tools/amikbd.py V S
   ```

   Expect `amikbd 0.3.x`, `sync=1`, and `held=0`. Use your board's COM port.
3. Test the full path:

   ```powershell
   python tools/amipush.py ping
   python tools/amipush.py put devchain/README.md RAM:link-readme.txt
   ```

   A successful `put` means the Amiga received and wrote the bytes, checked their
   total size and CRC32, closed the partial file, renamed it to the destination,
   and displayed a matching acknowledgement. A firmware `OK` alone is insufficient.

For another boot disk, copy `build/LinkAgent` into its C: drawer, then run
`C:LinkAgent` from an Amiga Shell. Only run one instance. The agent needs roughly
24 KB plus its AmigaDOS process, stack, console and filesystem buffers. It does
not require AmigaBASIC, a serial cable, or an Amiga C runtime.

## The agent's window (LinkAgent 1.1)

```
LinkAgent 1.1   RECEIVING                              <- IDLE or RECEIVING
[ACK barcode]                                           <- read by the PC; do not cover
Receiving RAM:demo64.bin  20430/65536 (31%)  CRC32 1C5D59B1
Packet #6 D: OK - block written                         <- last packet: OK / REJECTED / DISK ERROR + reason
Packets 6  repeats 0  bad frames 0  saved 0 files, 0 bytes
```

Row 3 shows the file being received, or the last one as `Saved name size CRC32` or
`Failed name at n/size bytes`. Names longer than 26 characters show as `...` plus
their last 23. Row 4 gives the reason for every rejection, for example
`destination already exists`, `old .part file exists, delete it`,
`unexpected offset (block lost?)`, `file CRC32 mismatch, not saved` or
`disk write failed, transfer aborted`. Corrupt, overflowed and timed-out frames get
no ACK (the PC resends) but now appear on row 4 and in the `bad frames` count.
`repeats` counts packets the PC resent after a missed ACK; they are not applied
twice. The barcode is written before the status rows, so the status costs the PC
almost no time (~3% on a 64 KB transfer).

## Using it

```powershell
python tools/amipush.py put path/to/game RAM:game
python tools/amipush.py --block-size 1024 put path/to/file DF0:file
python tools/amipush.py abort
python tools/amipush.py quit
```

The destination directory must already exist. The agent refuses to overwrite an
existing destination or `.part` file. On an OFS disk, keep the final filename at
25 characters or fewer so the `.part` suffix fits the 30-character component limit.
Use short ASCII paths. Normal keyboard and keyboard-mouse control pass through
between transfer frames; avoid other keyboard clients during a transfer.

`abort` closes and deletes the agent's currently open partial file. A partial
file left by a reset or a failed final rename is preserved; inspect or remove it
from the Amiga Shell before trying that path again. `quit` restores the original
keyboard interrupt handler. Ctrl-C also exits if delivered to the agent process.
There is no remote execute command in this version; `tools/amirun.py` types the
command into the A500-Shell instead (see below).

Transfers are sequential in 2048-byte blocks (maximum 4086 data bytes). Every
frame has a CRC32; the final file has its own independent size and CRC32 check.
The sender retries a missing screen acknowledgement up to three times, and the
receiver replays the last result without applying a duplicate packet twice.
Invalid offsets, premature completion, disk errors and checksum failures never
publish the final filename. Measured on the A500 (amikbd 0.3.1, defaults): 64 KB in 32.6 s (~2000 bytes/s),
16 KB in 10 s, an 8 KB program in 5.5 s.

Speed settings and why (all measured on the A500):

| Change | ms per byte |
|---|---|
| Original (60 us bits, attention pulse, 100 ms host read stall per command) | 2.29 |
| Host serial read fixed (`read(in_waiting)`, not `read(256)`) | 1.29 |
| No attention pulse for transfer bytes (`CFG RAWATTN 0`) | 1.16 |
| 20 us bits for transfer bytes (`CFG RAWBIT 20`) | 0.86 |
| Agent starts the handshake before decoding, ~50 us pulse | 0.44 |

Bit times of 12..20 us passed CRC-checked transfers; 10 us and below corrupted frames
even though every byte was handshaked, and garbled bytes that decode as frame END
reach keyboard.device as random keystrokes. `amipush` therefore refuses `--bit-us`
below 12 and defaults to 20. Normal key commands always use 60 us; amipush/amirun
restore the firmware defaults when they exit.

Blocks are 4086 bytes and pipelined: block n+1 is sent while block n is acknowledged.
The receiver only accepts the next sequential offset, so an OK for a block confirms all
earlier ones; any missing or failed acknowledgement aborts the partial file and resends
the whole file stop-and-wait (`--no-pipeline` forces that mode). A screen acknowledgement
takes ~0.5 s: ~250 ms is the HDMI capture chain, the rest agent and console time.

The power LED remains available for monitoring but is not the return channel.
The screen decoder expects the normal Workbench blue/white console palette and
character widths of 4..36 capture pixels. It scans the whole frame, validates
framing and an XOR check, and requires the same stripe on three scanlines.
An obscured window or changed palette stops the transfer instead of reporting
success. Camera URLs can be changed with `--snapshot-url`; COM ports with `--port`.

## Compile and run C programs (vbcc)

```powershell
python tools/amirun.py devchain/examples/hello/hello.c          # build, send, run, snapshot
python tools/amirun.py src/main.c src/gfx.c --name game --args "-v" --wait 8
python tools/amirun.py hello.c --build-only                     # just devchain/out/hello
```

`amirun` compiles with `vc +a500 -O2`, rejects any hunk type Kickstart 1.3 cannot
load, types `delete ram:NAME` into the A500-Shell (LinkAgent never overwrites),
sends the program to `RAM:NAME`, types `ram:NAME args`, waits `--wait` seconds and
saves `captures/run-NAME.jpg`. Program output appears in the A500-Shell, so the
snapshot is the result. Assembly sources (`.s`) can be mixed with C.

Toolchain, all project-local under `devchain/vendor/` (not in the repository; download them):

* `vbcc/`: official vbcc 0.9h win64 binaries (vc, vbccm68k, vasm, vlink) and the
  m68k-kick13 target (startup code, vc.lib, amiga.lib, KS 1.3 prototypes).
* `NDK3.2/`: Hyperion's free NDK 3.2 R4 (Aminet dev/misc/NDK3.2) for structure
  headers such as `exec/execbase.h`.
* `vbcc/config/a500`: copy of `devchain/config/a500` (vbcc licence, not MIT), vbcc's stock `kick13` config with `NDK3.2/Include_H` added after
  the kick13 headers, so only 1.3 functions are prototyped. Calling a V36+
  function is therefore a compile error, not a crash on the A500.

To use `vc` directly, set `VBCC` to `devchain\vendor\vbcc` and put its `bin` on
`PATH`. Kickstart 1.3 has 512 KB chip RAM plus 512 KB slow RAM on the test machine; `printf`
and the rest of vc.lib work under 1.3.

Boot disk layout: the startup script runs LinkAgent in a 5-line strip at the top
and opens `A500-Shell` below it with keyboard focus. Do not cover the strip. To
update the agent, `put` the new build to `DF0:C/LinkAgent.new` and reset; the
startup script installs it before launching.

## Build and validation

From the project root:

```powershell
python devchain/build.py
python devchain/build.py --workbench path\to\your\Workbench1.3.adf
python -m unittest discover -s devchain/tests -v
arduino-cli compile -b arduino:avr:leonardo firmware/amikbd
```

Building uses vasm (standalone in `vendor/vasm/`, or the copy bundled with vbcc) with `-kick1hunks` and rejects any hunk type Kickstart 1.3 cannot load. ADF creation uses the installed `amitools`.
The tests need Unicorn 2.1.4 and amitools:
`python -m pip install unicorn==2.1.4 amitools`.
The PC sender needs pyserial, Pillow and NumPy.

Tests execute the actual assembled 68000 code with DOS, Exec and CIA API stubs.
They cover byte escaping, all 256 byte values, odd-sized blocks, empty files,
CRC vectors, corrupt frames, resync, oversize packets, duplicate retries, sequence
wrap, offsets, whole-file CRC, short writes, abort, overwrite protection,
keyboard pass-through and interrupt restoration. The real assembly's ANSI
response is rasterized and JPEG-compressed to test screen decoding at several
scales. These tests do not emulate Kickstart scheduling, physical CIA timing,
the Gotek, or the HDMI capture chain. A live `ping` and small RAM: transfer remain
the hardware acceptance checks.

## Wire protocol

USB CDC: `RAWHEX <2..192 hexadecimal characters>` sends 1..96 decoded keyboard
bytes using the usual bus rotation. It validates the full string before sending.
`OK` means that all bytes received keyboard-level handshakes.

On the keyboard bus (values before firmware rotation):

* `70`: start/restart a frame.
* `71`: end a frame, return to keyboard pass-through.
* `72`: escape; next byte is XOR 80.
* `F9`: lost sync; discard the current frame.
* Escape data values 70, 71, 72, F9, FD and FE.

Frame body: `sequence.u8 command.u8 args... crc32.be32`. CRC32 is the zlib/IEEE
CRC over sequence, command and arguments. Maximum decoded body including CRC
is 4096 bytes. Multibyte integers are big-endian and may be unaligned.

| Command | Arguments | Effect |
|---|---|---|
| P | none | Ping |
| B | size.u32, file_crc32.u32, remaining bytes = ASCII path | Begin new `.part` file |
| D | offset.u32, remaining bytes = data | Write at the next sequential offset |
| E | none | Verify size/CRC, close, rename to final path |
| A | none | Abort the open partial file |
| Q | none | Acknowledge, wait 2 seconds, restore keyboard and exit |

The screen stripe contains five bytes, MSB first, one console space per bit:
`A5 sequence result (A5 XOR sequence XOR result) 5A`. An inverse space is one;
a normal space is zero. Result 0=success, 1=invalid request/state, 2=disk/path
error. Corrupt or incomplete frames get no acknowledgement. The sender derives
its next sequence from the currently visible stripe to reject stale replies.

The interrupt buffers bytes in an 8192-entry ring. CRC, disk I/O and screen writes
run in task context with interrupts enabled. A stalled wire frame is discarded
after about three seconds. The open file persists until E, A, Q or process exit.

CIA ownership follows the resource API; the original SP handler is temporarily
removed, saved, called for ordinary keys and restored on clean shutdown. This
is intentionally a dedicated keyboard transfer tool, not a general keyboard
extension. References: [Commodore CIA resource autodoc](https://d0.se/autodocs/cia.resource/AddICRVector),
[AmigaOS CIA resource documentation](https://wiki.amigaos.net/wiki/CIA_Resource),
[console control sequences](https://wiki.amigaos.net/wiki/Console_Device).
