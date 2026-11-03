"""Execute the assembled receiver on a 68000, with DOS/Exec/CIA API stubs.
Run: python -m unittest discover -s devchain/tests -v
The stubs test our machine code, not Kickstart, video timing or physical wiring.
"""
import io
from pathlib import Path
import random
import re
import struct
import sys
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'devchain/vendor/python'))
sys.path.insert(0, str(ROOT / 'tools'))
from unicorn import Uc, UC_ARCH_M68K, UC_MODE_BIG_ENDIAN, UC_HOOK_CODE
from unicorn.m68k_const import *
from amitools.binfmt.BinFmt import BinFmt
from amitools.binfmt.Relocate import Relocate
from PIL import Image, ImageDraw
from amipush import frame, decode_ack, put, Link

class Machine:
    def __init__(self):
        image = BinFmt().load_image(str(ROOT / 'devchain/build/LinkAgent'))
        reloc = Relocate(image)
        bases = reloc.get_seq_addrs(0x10000)
        self.sym = {s.get_name().decode(): base + s.get_offset()
                    for base, seg in zip(bases, image.get_segments())
                    for s in seg.get_symtab().get_symbols()}
        self.u = Uc(UC_ARCH_M68K, UC_MODE_BIG_ENDIAN)
        self.u.ctl_set_cpu_model(UC_CPU_M68K_M68000)
        self.u.mem_map(0, 0x100000)
        self.u.mem_map(0xbfe000, 0x2000)
        self.u.mem_write(0x10000, bytes(reloc.relocate_one_block(0x10000)))
        self.u.reg_write(UC_M68K_REG_SR, 0x2000)
        self.u.mem_write(4, struct.pack('>I', 0x70000))
        self.set('dosbase', 0x80000)
        self.set('conh', 1)
        self.files = {}
        self.handles = {}
        self.console = []
        self.short_write = False
        self.owner = 0x90000
        self.u.mem_write(self.owner + 14, struct.pack('>II', 0x92000, 0x91000))
        self.passed = 0
        self.calls = []
        self.u.hook_add(UC_HOOK_CODE, self.hook, begin=0x6f000, end=0x91000)

    def reg(self, n):
        return self.u.reg_read(n)
    def set(self, name, value, size=4):
        self.u.mem_write(self.sym[name], value.to_bytes(size, 'big'))
    def get(self, name, size=4):
        return int.from_bytes(self.u.mem_read(self.sym[name], size), 'big')
    def cstr(self, addr):
        data = bytes(self.u.mem_read(addr, 256))
        return data.split(b'\0')[0].decode('ascii')
    def hook(self, u, addr, size, data):
        ret = None
        d1, d2, d3 = (self.reg(r) for r in (UC_M68K_REG_D1, UC_M68K_REG_D2, UC_M68K_REG_D3))
        if addr == 0x91000:
            self.passed += 1
            ret = 0
        elif 0x7f000 < addr < 0x80000:
            lvo = addr - 0x80000
            self.calls.append(lvo)
            if lvo == -30:
                name = self.cstr(d1)
                self.files[name] = bytearray()
                handle = 100 + len(self.handles)
                self.handles[handle] = name
                ret = handle
            elif lvo == -48:
                buf = bytes(u.mem_read(d2, d3))
                if d1 == 1:
                    self.console.append(buf)
                    ret = d3
                else:
                    ret = d3 // 2 if self.short_write else d3
                    self.files[self.handles[d1]].extend(buf[:ret])
            elif lvo == -36:
                self.handles.pop(d1, None)
                ret = 0  # KS 1.3 Close() returns nothing; d0 must not be trusted
            elif lvo == -84:
                ret = 42 if self.cstr(d1) in self.files else 0
            elif lvo == -90:
                ret = 0
            elif lvo == -72:
                self.files.pop(self.cstr(d1), None)
                ret = 1
            elif lvo == -78:
                source, dest = self.cstr(d1), self.cstr(d2)
                self.files[dest] = self.files.pop(source)
                ret = 1
            elif lvo == -198:
                ret = 0
            else:
                raise AssertionError(f'Unexpected DOS call {lvo}')
        elif addr in (0x70000 - 120, 0x70000 - 126):
            ret = 0
        elif addr == 0x70000 - 522:
            out = self.rawdofmt(self.reg(UC_M68K_REG_A0), self.reg(UC_M68K_REG_A1))
            u.mem_write(self.reg(UC_M68K_REG_A3), out + b'\0')
            ret = 0
        elif addr == 0x70000 - 498:
            ret = 0x60000
        elif addr == 0x60000 - 6:
            ret = self.owner
            if not self.owner:
                self.owner = self.reg(UC_M68K_REG_A1)
        elif addr == 0x60000 - 12:
            assert self.owner == self.reg(UC_M68K_REG_A1)
            self.owner = 0
            ret = 0
        if ret is not None:
            u.reg_write(UC_M68K_REG_D0, ret)
            sp = self.reg(UC_M68K_REG_A7)
            pc = int.from_bytes(u.mem_read(sp, 4), 'big')
            u.reg_write(UC_M68K_REG_A7, sp + 4)
            u.reg_write(UC_M68K_REG_PC, pc)

    def rawdofmt(self, fmt_addr, args):
        """exec RawDoFmt subset: %[-][0][width][l](d|u|x|X|s); no 'l' = 16-bit argument."""
        fmt = bytes(self.u.mem_read(fmt_addr, 256)).split(b'\0')[0]
        out, i = bytearray(), 0
        while i < len(fmt):
            spec = re.match(rb'%(-?)(0?)(\d*)(l?)([duxXs])', fmt[i:])
            if not spec:
                out.append(fmt[i])
                i += 1
                continue
            left, zero, width, long_, kind = spec.groups()
            if kind == b's':
                text = self.cstr(int.from_bytes(self.u.mem_read(args, 4), 'big')).encode()
                args += 4
            else:
                size = 4 if long_ else 2
                value = int.from_bytes(self.u.mem_read(args, size), 'big', signed=kind == b'd')
                args += size
                text = (b'%X' if kind in b'xX' else b'%d') % value
            pad = int(width or 0) - len(text)
            if pad > 0:
                text = text + b' ' * pad if left else (b'0' if zero else b' ') * pad + text
            out += text
            i += spec.end()
        return bytes(out)

    def call(self, name, **regs):
        for k, v in regs.items():
            self.u.reg_write(globals()['UC_M68K_REG_' + k.upper()], v)
        self.u.reg_write(UC_M68K_REG_A7, 0xf0000)
        self.u.mem_write(0xf0000, struct.pack('>I', 0xff000))
        self.u.emu_start(self.sym[name], 0xff000, count=5000000)
        assert self.reg(UC_M68K_REG_PC) == 0xff000, name + ' did not return'

    def wire(self, data):
        for i, b in enumerate(data):
            self.call('decode', d0=b)
            if i % 96 == 95:
                self.call('drain')
        self.call('drain')
    def command(self, seq, cmd, args=b''):
        self.wire(frame(seq, cmd, args))
        return self.get('result', 1)
    def begin(self, data, name='RAM:test', seq=1):
        return self.command(seq, 'B', struct.pack('>II', len(data), zlib.crc32(data)) + name.encode())


def stripe(m):
    """The most recent ACK stripe write (it always starts by positioning row 2)."""
    return next(w for w in reversed(m.console) if w.startswith(b'\x1b[2;1H'))


def acks(m):
    """All ACK stripe writes so far."""
    return [w for w in m.console if w.startswith(b'\x1b[2;1H')]


def status(m):
    """Rows 1, 3, 4 and 5 of the most recent status write, as plain text lines."""
    rows = {}
    for w in m.console:
        for row, text in re.findall(rb'\x1b\[([1345]);1H((?:(?!\x1b\[[1-5];1H).)*)', w, re.S):
            rows[int(row)] = re.sub(rb'\x1b\[[0-9;]*[A-Za-z]', b'', text).decode()
    return [rows.get(r, '') for r in (1, 3, 4, 5)]


def render_console(data, cell=16, height=18):
    """Rasterize the actual report's ANSI inverse-space output into a JPEG.

    Interprets SGR (ESC[..m) and cursor position (ESC[H, ESC[r;cH) like the console
    does, and draws the spaces written on row 2 (the stripe row).
    """
    row, attr, cells, i = 1, b'0', [], 0
    while i < len(data):
        esc = re.match(rb'\x1b\[([0-9;]*)([A-Za-z])', data[i:])
        if esc:
            params, cmd = esc.groups()
            if cmd == b'm':
                attr = params.split(b';')[-1] or b'0'
            elif cmd == b'H':
                row = int(params.split(b';')[0] or b'1')
            i += esc.end()
            continue
        if data[i] == 10:
            row += 1
        elif data[i] == 32 and row == 2:
            cells.append(attr)
        i += 1
    assert len(cells) == 40, len(cells)
    im = Image.new('RGB', (1280, 720), '#aaaaaa')
    draw = ImageDraw.Draw(im)
    for i, c in enumerate(cells):
        x0, x1 = round(120 + i * cell), round(120 + (i + 1) * cell) - 1
        draw.rectangle((x0, 150, x1, 150 + height), fill='#ffffff' if c == b'7' else '#0055aa')
    b = io.BytesIO()
    im.save(b, 'JPEG', quality=75)
    return Image.open(io.BytesIO(b.getvalue()))


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.m = Machine()

    def test_crc_machine_code(self):
        for payload in (b'', b'123456789', bytes(range(256)), random.Random(4).randbytes(1001)):
            self.m.u.mem_write(0x90000, payload or b'\0')
            self.m.call('crc', a0=0x90000, d0=len(payload), d1=0xffffffff)
            self.assertEqual(self.m.reg(UC_M68K_REG_D1) ^ 0xffffffff, zlib.crc32(payload))

    def test_binary_transfer_and_duplicate_retry(self):
        m = self.m
        data = bytes(range(256)) * 30 + b'odd'
        self.assertEqual(m.begin(data), 0)
        for i, offset in enumerate(range(0, len(data), 1001), 2):
            args = struct.pack('>I', offset) + data[offset:offset + 1001]
            self.assertEqual(m.command(i, 'D', args), 0)
            self.assertEqual(m.command(i, 'D', args), 0)
        self.assertNotIn('RAM:test', m.files)
        self.assertEqual(m.command(50, 'E'), 0)
        self.assertEqual(bytes(m.files['RAM:test']), data)
        self.assertEqual(m.command(50, 'E'), 0)

    def test_empty_file(self):
        self.assertEqual(self.m.begin(b''), 0)
        self.assertEqual(self.m.command(2, 'E'), 0)
        self.assertEqual(self.m.files['RAM:test'], b'')

    def test_wrong_offset_and_size(self):
        m = self.m
        m.begin(b'abc')
        self.assertEqual(m.command(2, 'D', struct.pack('>I', 1) + b'abc'), 1)
        self.assertEqual(m.command(3, 'E'), 1)
        self.assertEqual(m.get('received'), 0)
        self.assertNotIn('RAM:test', m.files)

    def test_whole_file_crc(self):
        m = self.m
        m.begin(b'abc')
        self.assertEqual(m.command(2, 'D', bytes(4) + b'xyz'), 0)
        self.assertEqual(m.command(3, 'E'), 1)
        self.assertNotIn('RAM:test', m.files)

    def test_corruption_resync_and_oversize(self):
        m = self.m
        good = frame(1, 'P')
        corrupt = bytearray(good)
        corrupt[2] ^= 4
        m.wire(corrupt)
        self.assertEqual(acks(m), [])
        self.assertIn('Corrupt frame ignored', status(m)[2])
        m.wire(good[:3] + b'\xf9' + good)
        self.assertEqual(m.get('result', 1), 0)
        n = len(acks(m))
        m.wire(b'\x70' + b'\0' * 4200 + b'\x71')
        self.assertEqual(len(acks(m)), n)
        # corrupt CRC, the frame cut off by $F9, and the oversize frame
        self.assertIn('bad frames 3', status(m)[3])
        self.assertEqual(m.command(2, 'P'), 0)

    def test_no_overwrite(self):
        m = self.m
        m.files['RAM:test'] = bytearray(b'keep')
        self.assertEqual(m.begin(b'new'), 2)
        self.assertEqual(m.files['RAM:test'], b'keep')
        del m.files['RAM:test']
        m.files['RAM:test.part'] = bytearray(b'keep partial')
        self.assertEqual(m.begin(b'new', seq=2), 2)
        self.assertEqual(m.files['RAM:test.part'], b'keep partial')

    def test_abort_and_short_write(self):
        m = self.m
        m.begin(b'abc')
        self.assertEqual(m.command(2, 'A'), 0)
        self.assertFalse(m.files)
        m.begin(b'abc', seq=3)
        m.short_write = True
        self.assertEqual(m.command(4, 'D', bytes(4) + b'abc'), 2)
        self.assertFalse(m.files)
        self.assertEqual(m.get('fileh'), 0)

    def test_sequence_wrap(self):
        m = self.m
        data = bytes(range(256)) + b'wrap'
        m.begin(data, seq=250)
        for i, byte in enumerate(data):
            self.assertEqual(m.command((251 + i) & 255, 'D', struct.pack('>I', i) + bytes([byte])), 0)
        self.assertEqual(m.command((251 + len(data)) & 255, 'E'), 0)
        self.assertEqual(m.files['RAM:test'], data)

    def test_screen_ack_from_assembled_report(self):
        for seq in (0, 112, 113, 114, 249, 255):
            self.m.command(seq, 'P')
            for scale in (8, 8.75, 16, 17.5):
                self.assertEqual(decode_ack(render_console(stripe(self.m), scale)), (seq, 0))
        self.assertIsNone(decode_ack(Image.new('RGB', (1280, 720), '#aaaaaa')))

    def test_hook_install_restore_and_keyboard_passthrough(self):
        m = self.m
        # Resource hooks are outside main DOS hook range.
        m.u.hook_add(UC_HOOK_CODE, m.hook, begin=0x5fff0, end=0x60000)
        m.call('install')
        self.assertEqual(m.owner, m.sym['ourint'])
        self.assertEqual(m.get('oldcode'), 0x91000)
        for reg, val in (('D2', 0x11223344), ('A2', 0x55667788), ('A5', 0xaabbccdd)):
            m.u.reg_write(globals()['UC_M68K_REG_' + reg], val)
        m.u.mem_write(0xbfec01, b'\xbf')  # decoded key $20 ('a')
        m.call('kbdint')
        self.assertEqual(m.passed, 1)
        self.assertEqual(m.reg(UC_M68K_REG_D2), 0x11223344)
        self.assertEqual(m.reg(UC_M68K_REG_A2), 0x55667788)
        self.assertEqual(m.reg(UC_M68K_REG_A5), 0xaabbccdd)
        m.call('uninstall')
        self.assertEqual(m.owner, 0x90000)

    def test_pc_sender_to_actual_receiver(self):
        m = self.m
        m.call('report')
        class Keyboard:
            def command(self, text, timeout=5):
                if not text.startswith('RAWHEX '):
                    raise AssertionError(text)
                m.wire(bytes.fromhex(text[7:]))
                return True, 'OK', []
        class VideoLink(Link):
            def read_ack(self):
                return decode_ack(render_console(stripe(m)))
        link = VideoLink(Keyboard())
        import tempfile
        payload = bytes(range(256)) * 18 + b'PC sender integration'
        with tempfile.TemporaryDirectory(dir=ROOT / 'devchain/build') as folder:
            source = Path(folder) / 'source.bin'
            source.write_bytes(payload)
            with patch('builtins.print'):
                put(link, source, 'RAM:from-pc', block_size=1001)
        self.assertEqual(m.files['RAM:from-pc'], payload)

    def test_reports_write_stripe_first_then_status(self):
        m = self.m
        for seq in (0x12, 0xA5, 0xFF):
            m.set('pktseq', seq, 1)
            before = len(m.console)
            m.call('report')
            first, second = m.console[before:]
            self.assertTrue(first.startswith(b'\x1b[2;1H'))
            self.assertLess(len(first), 220)
            self.assertEqual(decode_ack(render_console(first)), (seq, 0))
            self.assertNotIn(b'\x1b[2;1H', second)
            self.assertNotIn(b'\n', second)   # no line feeds: the 5-line window must never scroll
        for line in status(m):
            self.assertLess(len(line), 76)

    def test_status_shows_progress_saved_file_and_counters(self):
        m = self.m
        data = bytes(range(256)) * 20
        name = 'RAM:status-test'
        self.assertEqual(m.begin(data, name=name), 0)
        self.assertEqual(m.command(2, 'D', struct.pack('>I', 0) + data[:2048]), 0)
        row1, row3, row4, row5 = status(m)
        self.assertIn('RECEIVING', row1)
        self.assertIn(f'Receiving {name}  2048/{len(data)} (40%)', row3)
        self.assertIn('CRC32 %08X' % zlib.crc32(data), row3.upper())
        self.assertIn('Packet #2 D: OK - block written', row4)
        self.assertIn('Packets 2  repeats 0  bad frames 0  saved 0 files, 0 bytes', row5)
        self.assertEqual(m.command(3, 'D', struct.pack('>I', 2048) + data[2048:]), 0)
        self.assertEqual(m.command(4, 'E'), 0)
        row1, row3, row4, row5 = status(m)
        self.assertIn('IDLE', row1)
        self.assertIn(f'Saved {name}  {len(data)} bytes', row3)
        self.assertIn('saved, size and CRC32 verified', row4)
        self.assertIn(f'saved 1 files, {len(data)} bytes', row5)
        self.assertEqual(m.command(4, 'E'), 0)
        self.assertIn('repeated packet, result resent', status(m)[2])
        self.assertIn('repeats 1', status(m)[3])

    def test_status_explains_rejections_and_bad_frames(self):
        m = self.m
        m.files['RAM:taken'] = bytearray(b'x')
        self.assertEqual(m.begin(b'abc', name='RAM:taken'), 2)
        self.assertIn('DISK ERROR - destination already exists', status(m)[2])
        self.assertIn('Failed RAM:taken', status(m)[1])
        data = b'payload'
        self.assertEqual(m.command(5, 'B', struct.pack('>II', len(data), 0x12345678) + b'RAM:crc'), 0)
        self.assertEqual(m.command(6, 'D', struct.pack('>I', 0) + data), 0)
        self.assertEqual(m.command(7, 'E'), 1)
        self.assertIn('REJECTED - file CRC32 mismatch, not saved', status(m)[2])
        self.assertIn('RECEIVING', status(m)[0])
        self.assertEqual(m.command(8, 'D', struct.pack('>I', 99) + b'x'), 1)
        self.assertIn('unexpected offset', status(m)[2])
        wire = bytearray(frame(9, 'P'))
        wire[3] ^= 0x01
        m.wire(bytes(wire))
        self.assertIn('Corrupt frame ignored', status(m)[2])
        self.assertIn('bad frames 1', status(m)[3])
        self.assertEqual(m.command(10, 'A'), 0)
        self.assertIn('transfer aborted, .part deleted', status(m)[2])
        self.assertIn('Failed RAM:crc', status(m)[1])
        self.assertNotIn('RAM:crc.part', m.files)

    def test_long_names_are_shortened_on_screen(self):
        m = self.m
        name = 'RAM:' + 'd' * 10 + '/' + 'very-long-file-name-for-display.bin'
        m.begin(b'abc', name=name)
        row3 = status(m)[1]
        self.assertIn('...' + name[-23:], row3)
        self.assertLess(len(row3), 76)

    def test_pipelined_sender_recovers_from_corrupt_block(self):
        m = self.m
        m.call('report')
        frames = []
        class Keyboard:
            def command(self, text, timeout=5):
                data = bytearray.fromhex(text[7:])
                if data[:1] == b'\x70':
                    frames.append(1)
                # Corrupt the third frame (second D block) once, as a bad bit on the wire would.
                if len(frames) == 3 and len(data) > 40 and not frames[-1] == 'hit':
                    data[40] ^= 0x01
                    frames[-1] = 'hit'
                m.wire(bytes(data))
                return True, 'OK', []
        class VideoLink(Link):
            def read_ack(self):
                return decode_ack(render_console(stripe(m)))
        link = VideoLink(Keyboard(), timeout=0.5)
        import tempfile
        payload = bytes(range(256)) * 12
        with tempfile.TemporaryDirectory(dir=ROOT / 'devchain/build') as folder:
            source = Path(folder) / 'source.bin'
            source.write_bytes(payload)
            with patch('builtins.print') as out:
                put(link, source, 'RAM:recovered', block_size=1000)
        self.assertTrue(any('without pipelining' in str(c) for c in out.call_args_list))
        self.assertEqual(m.files['RAM:recovered'], payload)
        self.assertNotIn('RAM:recovered.part', m.files)

    def test_second_instance_keeps_existing_owner(self):
        m = self.m
        m.u.hook_add(UC_HOOK_CODE, m.hook, begin=0x5fff0, end=0x60000)
        m.u.mem_write(0x90000 + 10, struct.pack('>I', m.sym['intname']))
        m.call('install')
        self.assertEqual(m.reg(UC_M68K_REG_D0), 0xffffffff)
        self.assertEqual(m.owner, 0x90000)

    def test_ring_overflow_rejects_frame_and_recovers(self):
        m = self.m
        m.call('decode', d0=0x70)
        for _ in range(8200):
            m.call('decode', d0=0)
        m.call('drain')
        self.assertFalse(acks(m))
        self.assertIn('bad frames 1', status(m)[3])
        self.assertEqual(m.command(1, 'P'), 0)

if __name__ == '__main__':
    unittest.main()
