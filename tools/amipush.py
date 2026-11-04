# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Keith Monahan
"""Checksummed PC -> A500 files over amikbd RAWHEX, screen barcode replies.

python tools/amipush.py ping
python tools/amipush.py put local.bin RAM:local.bin
python tools/amipush.py abort
python tools/amipush.py quit
"""
import argparse
import io
from pathlib import Path
import struct
import time
import urllib.request
import zlib

import numpy as np
from PIL import Image

from amikbd import Amikbd, SNAPSHOT_URL

RESERVED = {0x70, 0x71, 0x72, 0xF9, 0xFD, 0xFE}
MAX_DATA = 4086


def frame(seq, command, args=b""):
    body = bytes([seq]) + command.encode("ascii") + args
    if len(command) != 1 or len(body) + 4 > 4096:
        raise ValueError("packet exceeds receiver limit")
    body += struct.pack(">I", zlib.crc32(body))
    wire = bytearray([0x70])
    for b in body:
        wire.extend((0x72, b ^ 0x80) if b in RESERVED else (b,))
    wire.append(0x71)
    return bytes(wire)


def decode_ack(image, near=None):
    """Locate a 40-cell stripe without fixed capture coordinates or font scale.

    Require matching framing, XOR check, valid result, and the same stripe on
    three nearby scanlines. Returns (sequence, result), or None. `near` is a
    scanline to try first (see scan_ack).
    """
    hit = scan_ack(np.asarray(image.convert("L")), near)
    return hit and hit[:2]


def scan_ack(gray, near=None):
    """Like decode_ack on a grayscale array; returns (sequence, result, y) or None.

    With `near`, rows within 24 px of it are scanned first; the whole frame is
    scanned only if the stripe is not there.
    """
    height = gray.shape[0]
    rows = range(2, height - 2, 2)
    if near is not None:
        hit = _scan_rows(gray, [y for y in rows if abs(y - near) <= 24])
        if hit:
            return hit
        rows = [y for y in rows if abs(y - near) > 24]
    return _scan_rows(gray, rows)


def _scan_rows(gray, rows):
    width = gray.shape[1]
    found = {}
    # Blue/white Workbench palette, and either polarity of inverse video.
    for y in rows:
        row = gray[y] > 145
        edges = np.flatnonzero(row[1:] != row[:-1]) + 1
        for i in range(len(edges) - 6):
            edge = int(edges[i])
            cell = float(edges[i + 1] - edge)
            if not 4 <= cell <= 36:
                continue
            # The first white cell can merge with a light window border.
            if abs(edges[i + 2] - edges[i + 1] - cell) > 1.5:
                continue
            for w in (cell, cell - 0.25, cell + 0.25, cell - 0.5, cell + 0.5):
                for x in (edge, edge - w):
                    xs = (x + (np.arange(40) + 0.5) * w).astype(int)
                    if xs[0] < 0 or xs[-1] >= width:
                        continue
                    bits = row[xs]
                    if not bits[0]:
                        bits = ~bits
                    b = np.packbits(bits).tobytes()
                    if b[0] != 0xA5 or b[4] != 0x5A or b[2] > 2:
                        continue
                    if b[3] != (0xA5 ^ b[1] ^ b[2]):
                        continue
                    key = (round(x), round(w * 2), b[1], b[2])
                    ys = found.setdefault(key, set())
                    ys.add(y)
                    if len(ys) >= 3 and max(ys) - min(ys) <= 24:
                        return b[1], b[2], y
    return None


class Link:
    def __init__(self, keyboard, snapshot_url=SNAPSHOT_URL, timeout=15, retries=3):
        self.keyboard = keyboard
        self.url = snapshot_url
        self.timeout = timeout
        self.retries = retries
        self.row = None   # scanline where the stripe was last found
        current = self.read_ack()
        if current is None:
            raise RuntimeError("No LinkAgent ACK stripe visible. Start C:LinkAgent and expose its window.")
        # Never accept the stripe left by a previous invocation as a new reply.
        self.seq = (current[0] + 1) & 255

    def read_ack(self):
        with urllib.request.urlopen(self.url, timeout=5) as r:
            data = r.read()
        hit = scan_ack(np.asarray(Image.open(io.BytesIO(data)).convert("L")), self.row)
        if hit is None:
            return None
        self.row = hit[2]
        return hit[:2]

    def transmit(self, wire):
        for i in range(0, len(wire), 96):
            ok, reply, _ = self.keyboard.command("RAWHEX " + wire[i:i + 96].hex(), timeout=5)
            if not ok:
                return False
        return True

    def send(self, cmd, args=b""):
        """Transmit one packet without waiting; returns its sequence number."""
        seq = self.seq
        self.seq = (self.seq + 1) & 255
        if not self.transmit(frame(seq, cmd, args)):
            raise RuntimeError(f"keyboard link failed while sending {cmd}")
        return seq

    def wait(self, accept, timeout=None):
        """Poll the screen until the stripe shows one of the `accept` sequences.

        Returns (sequence, result), or None on timeout.
        """
        deadline = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < deadline:
            try:
                ack = self.read_ack()
            except (OSError, ValueError):
                ack = None
            if ack is not None and ack[0] in accept:
                return ack
            time.sleep(0.02)
        return None

    def command(self, cmd, args=b""):
        """Stop-and-wait: send, wait for the ACK, retransmit the same packet if none comes."""
        seq = self.seq
        self.seq = (self.seq + 1) & 255
        wire = frame(seq, cmd, args)
        for attempt in range(self.retries):
            if self.transmit(wire):
                ack = self.wait({seq})
                if ack is not None:
                    if ack[1] != 0:
                        raise RuntimeError(f"Amiga rejected {cmd}: status {ack[1]} (1=request/state, 2=disk/path)")
                    return
            print(f"Retry {attempt + 1}/{self.retries}: no verified Amiga reply", flush=True)
        raise RuntimeError("Transfer stopped: no verified screen acknowledgement. Partial file may remain; use abort.")


class PipelineError(RuntimeError):
    pass


def put(link, local, remote, block_size=MAX_DATA, pipeline=True):
    path = remote.encode("ascii")
    if not 1 <= len(path) <= 180 or any(b < 32 for b in path):
        raise ValueError("Amiga path must be 1..180 printable ASCII bytes")
    if not 1 <= block_size <= MAX_DATA:
        raise ValueError(f"block size must be 1..{MAX_DATA}")
    # Keep a consistent source snapshot, including when it changes on disk later.
    data = Path(local).read_bytes()
    if len(data) > 0x7FFFFFFF:
        raise ValueError("File exceeds AmigaDOS 1.3 file size limit")
    crc = zlib.crc32(data)
    started = time.monotonic()
    if pipeline:
        try:
            _put_blocks(link, data, crc, path, block_size, pipelined=True)
        except PipelineError as e:
            print(f"{e}; aborting and resending without pipelining", flush=True)
            link.command("A")
            _put_blocks(link, data, crc, path, block_size, pipelined=False)
    else:
        _put_blocks(link, data, crc, path, block_size, pipelined=False)
    took = time.monotonic() - started
    print(f"Saved {remote}: {len(data)} bytes, CRC32 {crc:08X} (Amiga verified), "
          f"{took:.1f} s, {len(data) / took:.0f} bytes/s")


def _put_blocks(link, data, crc, path, block_size, pipelined):
    link.command("B", struct.pack(">II", len(data), crc) + path)
    offsets = range(0, len(data), block_size)
    if not pipelined:
        for offset in offsets:
            chunk = data[offset:offset + block_size]
            link.command("D", struct.pack(">I", offset) + chunk)
            print(f"{offset + len(chunk)}/{len(data)} bytes verified", flush=True)
    else:
        # One block in flight: send block n, then confirm block n-1 (or n). The receiver
        # only accepts the next sequential offset, so an OK for a block confirms all before it.
        pending = None
        for offset in offsets:
            chunk = data[offset:offset + block_size]
            seq = link.send("D", struct.pack(">I", offset) + chunk)
            if pending is not None:
                _confirm(link, {pending[0], seq}, len(data), pending[1])
            pending = (seq, offset + len(chunk))
        if pending is not None:
            _confirm(link, {pending[0]}, len(data), pending[1])
    link.command("E")


def _confirm(link, accept, total, end):
    ack = link.wait(accept)
    if ack is None:
        raise PipelineError("no screen acknowledgement for a pipelined block")
    if ack[1] != 0:
        raise PipelineError(f"pipelined block rejected (status {ack[1]})")
    print(f"{end}/{total} bytes verified", flush=True)


# Transfer bit time. 10 us corrupted data on the A500 (garbled bytes can leave a frame and
# reach keyboard.device as random keys); 12 us was the fastest verified, 20 us leaves margin.
DEFAULT_BIT_US = 20
MIN_BIT_US = 12


def open_keyboard(port=None, bit_us=DEFAULT_BIT_US):
    """Open amikbd, check firmware and sync, and select fast timing for RAWHEX."""
    if not MIN_BIT_US <= bit_us <= 60:
        raise ValueError(f"bit time must be {MIN_BIT_US}..60 us")
    kb = Amikbd(port, verbose=False)
    try:
        ok, version, _ = kb.command("V")
        if not ok or "amikbd 0.3." not in version:
            raise RuntimeError("Upload the bundled amikbd 0.3 firmware first (RAWHEX required).")
        if version.split()[-1] >= "0.3.1":
            kb.command(f"CFG RAWBIT {bit_us}")
            kb.command("CFG RAWATTN 0")
        ok, status, _ = kb.command("S")
        if not ok or "sync=1" not in status or "held=0" not in status:
            raise RuntimeError("Keyboard link must be synced, with no held keys. Use tools/amikbd.py X S.")
    except BaseException:
        close_keyboard(kb)
        raise
    return kb


def close_keyboard(kb):
    """Restore keyboard-spec RAWHEX timing and close the port."""
    try:
        kb.command("CFG RAWBIT 60")
        kb.command("CFG RAWATTN 1")
    finally:
        kb.ser.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port")
    ap.add_argument("--snapshot-url", default=SNAPSHOT_URL)
    ap.add_argument("--timeout", type=float, default=15)
    ap.add_argument("--block-size", type=int, default=MAX_DATA)
    ap.add_argument("--bit-us", type=int, default=DEFAULT_BIT_US,
                    help=f"keyboard bit time for transfers, {MIN_BIT_US}..60 (60 = keyboard spec)")
    ap.add_argument("--no-pipeline", action="store_true", help="wait for each block's ACK before the next")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("ping", "abort", "quit"):
        sub.add_parser(name)
    p = sub.add_parser("put")
    p.add_argument("local")
    p.add_argument("remote")
    args = ap.parse_args()
    kb = None
    try:
        kb = open_keyboard(args.port, args.bit_us)
        link = Link(kb, args.snapshot_url, args.timeout)
        if args.cmd == "put":
            put(link, args.local, args.remote, args.block_size, not args.no_pipeline)
        else:
            link.command({"ping": "P", "abort": "A", "quit": "Q"}[args.cmd])
            print("Amiga acknowledged " + args.cmd)
    except (OSError, RuntimeError, ValueError, UnicodeError) as e:
        ap.exit(1, str(e) + "\n")
    finally:
        if kb is not None:
            close_keyboard(kb)


if __name__ == "__main__":
    main()
