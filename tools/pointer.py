"""Locates the Workbench 1.3 mouse pointer (red arrow) in a captured frame.

Usage: python tools/pointer.py [image.jpg]   (no argument = fetch a fresh snapshot)
Prints the tip position (top-left of the red arrow) in capture pixels.
"""
import io
import os
import sys
import urllib.request

from PIL import Image

sys.path.insert(0, os.path.dirname(__file__))
from amikbd import SNAPSHOT_URL  # noqa: E402


def grab():
    with urllib.request.urlopen(SNAPSHOT_URL, timeout=10) as r:
        return Image.open(io.BytesIO(r.read())).convert("RGB")


def find_pointer(img):
    """Returns (x, y) of the arrow tip, or None. Red = strong R, weak G and B (orange has more G)."""
    w, h = img.size
    px = img.load()
    hits = [(x, y) for y in range(0, h, 1) for x in range(0, w, 2)
            if (lambda p: p[0] > 170 and p[1] < 90 and p[2] < 90)(px[x, y])]
    if len(hits) < 6:
        return None
    # The arrow is a compact blob; take the densest cluster around the median point.
    hits.sort()
    mx = sorted(p[0] for p in hits)[len(hits) // 2]
    my = sorted(p[1] for p in hits)[len(hits) // 2]
    near = [p for p in hits if abs(p[0] - mx) < 40 and abs(p[1] - my) < 40]
    return min(p[0] for p in near), min(p[1] for p in near)


# Capture pixels per keyboard-mouse step, measured on WB 1.3 at 1280x720 capture (2026-10-20).
STEP = {"x": (1.75, 178.0), "y": (1.275, 94.0)}   # (plain, Shift)


def plan_axis(d, plain, fast):
    """Splits a capture-pixel distance into (fast_steps, plain_steps)."""
    n_fast = int(abs(d) // fast)
    rest = abs(d) - n_fast * fast
    if rest > 0.75 * fast:   # one more fast step is cheaper than many plain ones
        n_fast += 1
        rest = 0
    return n_fast, int(round(rest / plain))


def goto(kb, tx, ty, tol=3, tries=5):
    """Closed-loop move of the pointer tip to capture pixel (tx, ty)."""
    import time
    for _ in range(tries):
        pos = find_pointer(grab())
        if not pos:
            return None
        dx, dy = tx - pos[0], ty - pos[1]
        if abs(dx) <= tol and abs(dy) <= tol:
            return pos
        for d, axis, neg, posk in ((dx, "x", "LEFT", "RIGHT"), (dy, "y", "UP", "DOWN")):
            if abs(d) <= tol:
                continue
            key = neg if d < 0 else posk
            nf, npl = plan_axis(d, *STEP[axis])
            if nf:
                kb.command(f"MOVE {key} {nf} FAST")
            if npl:
                kb.command(f"MOVE {key} {npl}")
        time.sleep(0.3)
    return find_pointer(grab())


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--goto":
        from amikbd import Amikbd
        print("pointer:", goto(Amikbd(verbose=False), int(sys.argv[2]), int(sys.argv[3])))
    else:
        img = Image.open(sys.argv[1]).convert("RGB") if len(sys.argv) > 1 else grab()
        pos = find_pointer(img)
        print("pointer:", pos if pos else "not found")
