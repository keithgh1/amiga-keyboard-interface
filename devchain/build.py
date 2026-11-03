"""Rebuild LinkAgent and optionally a bootable Gotek ADF from an owned WB 1.3 image."""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "devchain/build"


def run(*args):
    subprocess.run([str(a) for a in args], cwd=ROOT, check=True)


def xdf(image, *args):
    run(sys.executable, "-m", "amitools.tools.xdftool", image, *args)


KS13_HUNKS = {0x3E9: "CODE", 0x3EA: "DATA", 0x3EB: "BSS", 0x3EC: "RELOC32",
              0x3F0: "SYMBOL", 0x3F1: "DEBUG", 0x3F2: "END"}


def check_ks13_hunks(path):
    """Walk a HUNK_HEADER executable; fail on hunk types KS 1.3 LoadSeg rejects."""
    data = path.read_bytes()
    pos = 0

    def u32():
        nonlocal pos
        pos += 4
        return int.from_bytes(data[pos - 4:pos], "big")

    if u32() != 0x3F3 or u32() != 0:
        raise RuntimeError(f"{path}: not a plain HUNK_HEADER executable")
    count, first, last = u32(), u32(), u32()
    pos += 4 * (last - first + 1)
    seen = 0
    while seen < count:
        kind = u32() & 0x3FFFFFFF
        if kind not in KS13_HUNKS:
            raise RuntimeError(f"{path}: hunk type {kind:#x} at offset {pos - 4} is not loadable by KS 1.3")
        if kind in (0x3E9, 0x3EA, 0x3F1):
            n = u32()
            pos += 4 * n
        elif kind == 0x3EB:
            u32()
        elif kind == 0x3EC:
            while (n := u32()):
                pos += 4 * (n + 1)
        elif kind == 0x3F0:
            while (n := u32()):
                pos += 4 * (n + 1)
        elif kind == 0x3F2:
            seen += 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workbench", type=Path, help="path to your original bootable Workbench 1.3 ADF")
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    # Standalone vasm if present, else the copy bundled with vbcc (same output)
    vasm = next((p for p in (ROOT / "devchain/vendor/vasm/vasmm68k_mot.exe",
                             ROOT / "devchain/vendor/vbcc/bin/vasmm68k_mot.exe") if p.exists()), None)
    if not vasm:
        sys.exit("vasm not found: install vbcc into devchain/vendor/vbcc (see devchain/README.md)")
    # -kick1hunks: no HUNK_DREL32/RELOC32SHORT, which KS 1.3 LoadSeg rejects
    run(vasm, "-m68000", "-Fhunkexe", "-kick1hunks",
        "-L", OUT / "LinkAgent.lst", "-o", OUT / "LinkAgent", ROOT / "devchain/agent/LinkAgent.s")
    check_ks13_hunks(OUT / "LinkAgent")
    if args.workbench:
        original = args.workbench.resolve()
        target = OUT / "LinkAgent-WB13.adf"
        if original == target:
            ap.error("The source ADF must be separate from the generated output")
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        shutil.copyfile(original, target)
        # copyfile can inherit no read-only flag; explicitly retain writable output.
        startup = OUT / "startup-original"
        if startup.exists():
            startup.unlink()
        xdf(target, "read", "s/startup-sequence", startup)
        text = startup.read_text(encoding="latin1")
        lines = text.splitlines()
        # amikbd types US layout; a non-US SetMap garbles punctuation such as ':'.
        for i, line in enumerate(lines):
            if line.strip().lower().startswith("sys:system/setmap"):
                comment = line[line.index(";"):] if ";" in line else ""
                lines[i] = "SYS:System/SetMap usa1 " + comment
        for i, line in enumerate(lines):
            if line.strip().lower().startswith("endcli"):
                lines[i:i] = ["; Keyboard transfer agent: install a pending update, run it from RAM:",
                              "If EXISTS C:LinkAgent.new",
                              "  Copy C:LinkAgent.new C:LinkAgent",
                              "  Delete C:LinkAgent.new",
                              "EndIf",
                              "Copy C:LinkAgent RAM:LinkAgent", "Run >NIL: RAM:LinkAgent",
                              "; Shell below the agent strip, opened last so it takes keyboard focus",
                              "Wait 2",
                              "NewShell CON:0/72/640/184/A500-Shell"]
                break
        else:
            raise RuntimeError("No EndCLI found; inspect this Workbench startup before adding agent")
        generated = OUT / "startup-sequence"
        generated.write_bytes(("\n".join(lines) + "\n").encode("latin1"))
        xdf(target, "write", OUT / "LinkAgent", "c/LinkAgent")
        xdf(target, "write", startup, "s/LinkAgent-startup-original")
        xdf(target, "delete", "s/startup-sequence")
        xdf(target, "write", generated, "s/startup-sequence")
        xdf(target, "boot", "show")
        check = OUT / "LinkAgent-readback"
        if check.exists():
            check.unlink()
        xdf(target, "read", "c/LinkAgent", check)
        assert check.read_bytes() == (OUT / "LinkAgent").read_bytes()
        assert hashlib.sha256(original.read_bytes()).hexdigest() == digest
        print("Gotek image:", target)
        print("Original Workbench SHA256 (unchanged):", digest)
    print("Receiver:", OUT / "LinkAgent")


if __name__ == "__main__":
    main()
