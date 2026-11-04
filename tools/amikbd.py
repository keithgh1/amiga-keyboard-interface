# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Keith Monahan
"""Host client for the amikbd firmware.

Usage:
    python tools/amikbd.py "S"                      # one or more commands, each waits for OK/ERR
    python tools/amikbd.py "T dir\\n" "WAITDISK"
    python tools/amikbd.py --snap out.jpg "K LAMIGA+M"   # grab a frame afterwards
    python tools/amikbd.py -i                       # interactive
"""
import argparse
import os
import sys
import time
import urllib.request

import serial
from serial.tools import list_ports

VID, PID = 0x2341, 0x8036
# Any HTTP endpoint that returns the current Amiga frame as a JPEG.
SNAPSHOT_URL = os.environ.get("AMIGA_SNAPSHOT_URL", "http://localhost:8080/snapshot")


def find_port():
    for p in list_ports.comports():
        if p.vid == VID and p.pid == PID:
            return p.device
    raise SystemExit("amikbd: Pro Micro (2341:8036) not found")


class Amikbd:
    def __init__(self, port=None, verbose=True):
        # Windows sometimes reports the CDC port missing for a moment after the previous close.
        for attempt in range(6):
            try:
                self.ser = serial.Serial(port or find_port(), 115200, timeout=0.1)
                break
            except serial.SerialException:
                if attempt == 5:
                    raise
                time.sleep(0.5)
        self.ser.dtr = True
        self.verbose = verbose
        self.buf = b""   # bytes read past the last reply; kept for the next call
        time.sleep(0.2)
        self.ser.reset_input_buffer()

    def command(self, line, timeout=120.0):
        """Sends one command line; returns (ok, reply, events)."""
        self.ser.write((line + "\n").encode("ascii"))
        events = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            if b"\n" not in self.buf:
                # read(256) would sit out the whole timeout unless 256 bytes arrive
                self.buf += self.ser.read(max(1, self.ser.in_waiting))
            while b"\n" in self.buf:
                raw, self.buf = self.buf.split(b"\n", 1)
                text = raw.decode("ascii", "replace").strip()
                if not text:
                    continue
                if text.startswith("#"):
                    events.append(text)
                    if self.verbose:
                        print(text)
                    continue
                if self.verbose:
                    print(text)
                return text.startswith("OK"), text, events
        return False, "ERR HOSTTIMEOUT", events


def snapshot(path):
    with urllib.request.urlopen(SNAPSHOT_URL, timeout=10) as r, open(path, "wb") as f:
        f.write(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("commands", nargs="*")
    ap.add_argument("-p", "--port")
    ap.add_argument("-i", "--interactive", action="store_true")
    ap.add_argument("--snap", help="save a snapshot after the commands")
    ap.add_argument("--snap-delay", type=float, default=0.5)
    args = ap.parse_args()

    kb = Amikbd(args.port)
    ok_all = True
    for c in args.commands:
        ok, _, _ = kb.command(c)
        ok_all &= ok
    if args.interactive:
        try:
            for line in sys.stdin:
                if line.strip():
                    kb.command(line.rstrip("\r\n"))
        except KeyboardInterrupt:
            pass
    if args.snap:
        time.sleep(args.snap_delay)
        snapshot(args.snap)
        print(f"snapshot: {args.snap}")
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
