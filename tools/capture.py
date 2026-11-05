# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Keith Monahan
"""Feeds the Amiga's screen from a Windows video capture device to the other tools.

Usage:
  python tools/capture.py --list                  show capture devices
  python tools/capture.py                         use the only device, or the one in AMIGA_CAPTURE_DEVICE
  python tools/capture.py --device XR1            pick a device by (part of) its name
  python tools/capture.py --snap captures/now.jpg save the current frame (while the above is running)
Leave it running. Frames are 1280x720 at http://localhost:8080/snapshot, the tools' default;
/stream is a live view for a browser. Needs ffmpeg on PATH.
"""
import argparse
import collections
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WIDTH, HEIGHT = 1280, 720


def list_devices():
    out = subprocess.run(["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                         capture_output=True, text=True, errors="replace").stderr
    return re.findall(r'\] "([^"]+)" \(video\)', out)


def pick_device(want, devices):
    if not devices:
        sys.exit("capture: no video capture devices found")
    if not want:
        if len(devices) == 1:
            return devices[0]
        sys.exit("capture: several devices, choose one with --device:\n  " + "\n  ".join(devices))
    exact = [d for d in devices if d.lower() == want.lower()]
    part = [d for d in devices if want.lower() in d.lower()]
    found = exact or part
    if len(found) != 1:
        sys.exit(f"capture: {'no' if not found else 'more than one'} device matches {want!r}:\n  "
                 + "\n  ".join(devices))
    return found[0]


class Grabber:
    """Runs ffmpeg on the device and keeps the newest JPEG frame."""

    def __init__(self, device, size, fps, quality):
        self.device, self.size, self.fps, self.quality = device, size, fps, quality
        self.frame, self.count = None, 0
        self.cond = threading.Condition()
        self.proc = None

    def command(self, size):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "dshow", "-rtbufsize", "64M"]
        if size:
            cmd += ["-video_size", size, "-framerate", "30"]
        return cmd + ["-i", f"video={self.device}",
                      "-vf", f"fps={self.fps},scale={WIDTH}:{HEIGHT}:flags=bicubic,format=yuvj420p",
                      "-c:v", "mjpeg", "-q:v", str(self.quality), "-f", "image2pipe", "-"]

    def start(self):
        # Ask for the requested size first; some devices only accept their own default mode.
        for size in ([self.size, None] if self.size else [None]):
            self.proc = subprocess.Popen(self.command(size), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.errors = collections.deque(maxlen=20)
            threading.Thread(target=self.read, args=(self.proc,), daemon=True).start()
            threading.Thread(target=self.log, args=(self.proc, self.errors), daemon=True).start()
            if self.wait_frame(0, timeout=8) is not None:
                return f"{size} at 30 fps" if size else "device default mode"
            self.proc.kill()
            self.proc.wait()
            time.sleep(0.2)
            if size:
                print(f"capture: {size} at 30 fps not accepted, using the device's default mode", flush=True)
        sys.exit(f"capture: could not read from {self.device!r}: " + ("\n".join(self.errors) or "no frames"))

    @staticmethod
    def log(proc, errors):
        for line in proc.stderr:
            errors.append(line.decode(errors="replace").rstrip())

    def read(self, proc):
        buf = b""
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                return
            buf += chunk
            while True:
                s = buf.find(b"\xff\xd8")
                e = buf.find(b"\xff\xd9", s + 2) if s >= 0 else -1
                if e < 0:
                    break
                with self.cond:
                    self.frame, self.count = buf[s:e + 2], self.count + 1
                    self.cond.notify_all()
                buf = buf[e + 2:]

    def wait_frame(self, after, timeout=5):
        """Returns the first frame newer than frame number `after`."""
        with self.cond:
            self.cond.wait_for(lambda: self.count > after, timeout)
            return self.frame if self.count > after else None

    def stop(self):
        if self.proc:
            self.proc.kill()


def make_handler(grab):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/snapshot":
                # A frame captured after the request, so the caller never sees the screen from before its action.
                frame = grab.wait_frame(grab.count)
                if frame is None:
                    return self.send_error(503, "no frame from capture device")
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(frame)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(frame)
            elif path == "/stream":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                n = grab.count
                try:
                    while True:
                        frame = grab.wait_frame(n)
                        if frame is None:
                            continue
                        n = grab.count
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
            else:
                self.send_error(404)

        def log_message(self, *args):
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list capture devices and exit")
    ap.add_argument("--device", default=os.environ.get("AMIGA_CAPTURE_DEVICE"), help="device name or part of it")
    ap.add_argument("--size", default="1920x1080", help="input size to request from the device")
    ap.add_argument("--fps", type=int, default=25)
    ap.add_argument("--quality", type=int, default=3, help="JPEG quality, 2 (best) to 31")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--snap", metavar="FILE", help="save the current frame from the running capture.py, then exit")
    args = ap.parse_args()

    if args.snap:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/snapshot", timeout=10) as r:
                data = r.read()
        except OSError as e:
            sys.exit(f"capture: no frame ({e}); is capture.py running?")
        with open(args.snap, "wb") as f:
            f.write(data)
        print(f"snapshot: {args.snap}")
        return
    if not shutil.which("ffmpeg"):
        sys.exit("capture: ffmpeg not found on PATH (winget install Gyan.FFmpeg)")
    devices = list_devices()
    if args.list:
        print("\n".join(devices) or "no video capture devices found")
        return
    device = pick_device(args.device, devices)
    grab = Grabber(device, args.size, args.fps, args.quality)
    mode = grab.start()
    # Loopback only, on IPv4 and IPv6: Windows tries ::1 first for "localhost" and stalls 2 s without it.
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        cls = type("Server", (ThreadingHTTPServer,), {"address_family": family, "daemon_threads": True})
        try:
            server = cls((host, args.port), make_handler(grab))
        except OSError as e:
            if family == socket.AF_INET:
                grab.stop()
                sys.exit(f"capture: port {args.port}: {e}")
            continue
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"capture: {device} ({mode}) -> http://localhost:{args.port}/snapshot   Ctrl+C to stop", flush=True)
    try:
        while grab.proc.poll() is None:
            time.sleep(0.5)
        time.sleep(0.2)
        print("capture: device stopped:", "\n".join(grab.errors) or "no reason given", flush=True)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    finally:
        grab.stop()


if __name__ == "__main__":
    main()
