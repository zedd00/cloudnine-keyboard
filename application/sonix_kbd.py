"""
Sonix SN32F240B keyboard protocol library for Cloudnine ErgoFSK C989.
Transport: 64-byte HID Feature Reports via hidapi (hidraw backend).
"""
import ctypes
import ctypes.util
import struct
import time

# ── hidapi bindings ──────────────────────────────────────────────────────────
_LIB_PATH = ctypes.util.find_library("hidapi-hidraw")
if not _LIB_PATH:
    raise OSError("libhidapi-hidraw not found")
_HIDAPI = ctypes.CDLL(_LIB_PATH)
_HIDAPI.hid_open.argtypes = [ctypes.c_ushort, ctypes.c_ushort, ctypes.c_wchar_p]
_HIDAPI.hid_open.restype = ctypes.c_void_p
_HIDAPI.hid_close.argtypes = [ctypes.c_void_p]
_HIDAPI.hid_close.restype = ctypes.c_int
_HIDAPI.hid_send_feature_report.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
_HIDAPI.hid_send_feature_report.restype = ctypes.c_int
_HIDAPI.hid_get_feature_report.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
_HIDAPI.hid_get_feature_report.restype = ctypes.c_int
_HIDAPI.hid_error.argtypes = [ctypes.c_void_p]
_HIDAPI.hid_error.restype = ctypes.c_wchar_p
_HIDAPI.hid_read_timeout.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int]
_HIDAPI.hid_read_timeout.restype = ctypes.c_int

# ── protocol constants ───────────────────────────────────────────────────────
REPORT_SIZE = 64          # payload per feature report (excluding report-id byte)
FRAME_SIZE  = 65          # report-id(1) + payload(64)
REPORT_ID   = 0x00

# opcodes (2-byte, big-endian at offset 0x00-0x01); semantics per docs/PAYLOAD_OPCODES.md
OP_READ_INFO      = 0x0405   # read device info/config page A (0x9000, read-op)
OP_READ_COLORS    = 0x0412   # read color/effect config (0x9340, read-op)
OP_CHECKSUM       = 0x0402   # request checksum after upload
OP_WRITE_COLORS   = 0x0413   # upload color/effect config (0x9340, write-op)
OP_WRITE_KEYMAP   = 0x0411   # upload key remap table (0x90C0, write-op)
# The second of the firmware's two keymap banks. FUN_00002022
# (settings/export.txt:5266) picks the Fn layer from 0xA440+key_index*4, and the
# 0x0427 dispatcher FUN_00003bec reads the same address, so this region is the
# Fn layer and not "wheel ring B" as docs/PAYLOAD_OPCODES.md used to call it.
# Same 640-byte shape and the same 0x55AA0000 tail word as the Top layer
# (0xA440+636 = 0xA6BC, the Fn arming gate). NOT yet exercised on hardware.
OP_READ_FN        = 0x0426    # read the Fn keymap layer from 0xA440
OP_WRITE_FN       = 0x0427    # write the Fn keymap layer to 0xA440
OP_WRITE_MACRO    = 0x0415   # upload macro data (0x97C0, write-op)
OP_READ_MACRO     = 0x0414   # read macro data back from 0x97C0 (read-op)
OP_WRITE_GAMING   = 0x0417   # upload gaming mode (0x9080, write-op)
OP_READ_STATUS    = 0x0418   # transfer lead: DAT_20000172 bit6 clear (flag toggle)
OP_READ_STATUS2   = 0x0419   # DAT_20000172 bit6 set (flag toggle)
OP_READ_STATUS3   = 0x04AB   # special: dispatch sets cmd state DAT_20000170=4
# 0xA400 is the SMART WHEEL indicator bank (the wheel is on a separate daughter
# board). It is NOT the side strips: those are LED1-LED19 on the main PCB and
# their write path is still unidentified. These were previously (mis)named
# *_SIDELIGHTS; that name has been removed everywhere to avoid confusion.
OP_READ_WHEEL    = 0x0424    # read wheel presets from flash 0xA400
OP_WRITE_WHEEL   = 0x0425    # write wheel presets to flash 0xA400

# status byte at offset 0x03 in ACK response
STATUS_OK = 0x01
STATUS_ERROR = 0xFF

# ── wait before reading a command back ──────────────────────────────────────
# This is a fix, and it is the fix for one specific thing: the `04 xx 00 00`
# replies. Do not let it be described as more than that.
#
# The device writes its verdict into payload offset 3 of the report it holds,
# and a GET_REPORT hands back whatever is in that buffer at the moment it is
# asked. Issue the read straight after the SET and you get our own byte echoed
# back, unpatched -- which reads exactly like a refusal and is not one.
#
# Measured on the wire, two usbmon captures of one apply each, same SETUP, same
# 64 payload bytes, same endpoint, every transfer status 0:
#
#     delay from the SET completing to the GET being submitted
#     ours, before this    17-26 us      (mean 20)     -- no delay at all
#     OEM                  10173-10237 us (mean 10195)
#     ours, at 0.010       10077-10135 us (mean 10095)
#
# The OEM's is tight to within 60 us over sixteen commands, so it is a
# deliberate 10 ms wait. At 0.010 the header reads 0x01 and 0x0402 returns a
# computed checksum that matches ours. Before it, not one apply in 949 ever
# produced a 0x01 header.
#
# This is NOT an acceptance test. 72 of the 75 applies the device accepted came
# back with a 0x00 header, so that byte never tracked acceptance in either
# direction. `0x0402 == 0x01` is the signal, and it is checked in cloudnine_app.
#
# 0.0 restores the old behaviour exactly, so this is reversible without code
# edits. It costs ~10 ms per read-back command, about 40 ms per apply.
# See docs/investigation.md.
ACK_DELAY_S = 0.010

# ── frame helpers ────────────────────────────────────────────────────────────
def _make_frame(opcode: int, pages: int = 0, data: bytes = b"") -> bytes:
    """Build a 64-byte command frame."""
    buf = bytearray(REPORT_SIZE)
    buf[0] = (opcode >> 8) & 0xFF
    buf[1] = opcode & 0xFF
    # byte 8 = page count (visible in echo response)
    buf[8] = pages & 0xFF
    buf[9] = (pages >> 8) & 0xFF
    # bytes 28-29 = page count u16 LE (firmware reads from here)
    buf[28] = pages & 0xFF
    buf[29] = (pages >> 8) & 0xFF
    if data:
        buf[10:10+len(data)] = data[:REPORT_SIZE-10]
    return bytes(buf)

def _parse_ack(frame: bytes) -> tuple:
    """Parse ACK response, return (status, page_count, opcode)."""
    opcode = (frame[0] << 8) | frame[1]
    status = frame[3]
    pages  = (frame[8] | (frame[9] << 8)) & 0xFFFF
    return status, pages, opcode

def _parse_checksum(resp: bytes) -> int:
    """Extract 32-bit checksum from a 04 02 response (little-endian on this FW)."""
    if len(resp) < 8:
        raise RuntimeError(f"short checksum response: {resp.hex(' ')}")
    return struct.unpack_from("<I", resp, 4)[0]

def _checksum_type1(data: bytes) -> int:
    """Sonix checksum type-1: sum of all bytes as u32."""
    return sum(data) & 0xFFFFFFFF

# ── transport ────────────────────────────────────────────────────────────────
class Keyboard:
    def __init__(self, vid=0x0C45, pid=0x8006, serial=None):
        self.vid = vid
        self.pid = pid
        self._dev = None
        self._open(serial)

    def _open(self, serial):
        sn = ctypes.c_wchar_p(serial) if serial else None
        self._dev = _HIDAPI.hid_open(self.vid, self.pid, sn)
        if not self._dev:
            raise OSError(f"Cannot open device {self.vid:04x}:{self.pid:04x}")

    def close(self):
        if self._dev:
            _HIDAPI.hid_close(self._dev)
            self._dev = None

    def __del__(self):
        self.close()

    def _send_recv(self, frame: bytes) -> bytes:
        """Send 64-byte payload as feature report (report-id 0), read 64-byte reply."""
        # hidapi feature reports: first byte = report-id (0), then payload
        send_buf = bytes([REPORT_ID]) + frame
        r = _HIDAPI.hid_send_feature_report(self._dev, send_buf, FRAME_SIZE)
        if r < 0:
            raise OSError(f"hid_send_feature_report failed: {_HIDAPI.hid_error(self._dev)}")
        # The device patches its verdict into this report asynchronously, and
        # a GET issued too soon reads back our own unpatched byte -- see
        # ACK_DELAY_S. This is the measured OEM cadence, not a fudge factor.
        if ACK_DELAY_S:
            time.sleep(ACK_DELAY_S)
        recv_buf = ctypes.create_string_buffer(FRAME_SIZE)
        r = _HIDAPI.hid_get_feature_report(self._dev, recv_buf, FRAME_SIZE)
        if r < 0:
            raise OSError(f"hid_get_feature_report failed: {_HIDAPI.hid_error(self._dev)}")
        return recv_buf.raw[1:r]  # strip report-id byte

    def _send_only(self, frame: bytes) -> int:
        """Send 64-byte payload as feature report without reading a reply."""
        send_buf = bytes([REPORT_ID]) + frame
        r = _HIDAPI.hid_send_feature_report(self._dev, send_buf, FRAME_SIZE)
        if r < 0:
            raise OSError(f"hid_send_feature_report failed: {_HIDAPI.hid_error(self._dev)}")
        return r

    def _read_input(self, timeout_ms: int = 200) -> bytes:
        """Read one interrupt-IN report from the vendor interface (no report-id)."""
        recv_buf = ctypes.create_string_buffer(FRAME_SIZE)
        r = _HIDAPI.hid_read_timeout(self._dev, recv_buf, REPORT_SIZE, timeout_ms)
        if r < 0:
            raise OSError(f"hid_read_timeout failed: {_HIDAPI.hid_error(self._dev)}")
        return recv_buf.raw[:r]

    def _send_empty(self) -> bytes:
        """Send empty 64-byte feature report, read reply (for data page fetches)."""
        send_buf = ctypes.create_string_buffer(FRAME_SIZE)  # report-id=0 + 64 zero bytes
        r = _HIDAPI.hid_send_feature_report(self._dev, send_buf, FRAME_SIZE)
        if r < 0:
            raise OSError(f"hid_send_feature_report (empty) failed: {_HIDAPI.hid_error(self._dev)}")
        recv_buf = ctypes.create_string_buffer(FRAME_SIZE)
        r = _HIDAPI.hid_get_feature_report(self._dev, recv_buf, FRAME_SIZE)
        if r < 0:
            raise OSError(f"hid_get_feature_report (empty) failed: {_HIDAPI.hid_error(self._dev)}")
        return recv_buf.raw[1:r]

    def drain(self, n: int = 64) -> int:
        """Flush any pending transfer pages.

        A read request arms a state machine that emits one page per feature GET;
        if the host does not consume them, later sessions are blocked (lead
        answers status 0x00). Spraying empty feature reports clears it.
        """
        for i in range(n):
            try:
                self._send_empty()
            except OSError:
                return i
        return n

    # ── high-level commands ──────────────────────────────────────────────────
    def read_info(self) -> bytes:
        """Read device info. Returns the (2) data pages concatenated."""
        frame = _make_frame(OP_READ_INFO, pages=2)
        ack = self._send_recv(frame)
        status, _, op = _parse_ack(ack)
        if op == 0:
            # firmware may answer with data on the first GET instead of an ACK
            return ack
        data = b""
        for _ in range(2):
            time.sleep(0.05)
            page = self._send_empty()
            if page and page[2] == 0x02 and page[5] == 0xff:
                page = self._send_empty()
            data += page
        return data

    def read_colors(self, pages: int = 18) -> bytes:
        """Read back the current color/effect config (OP_READ_COLORS).

        Response framing (see docs/PAYLOAD_OPCODES.md §3, item 3 of the mapping
        plan): after SEND 04 12 the device either answers the first GET with an
        ACK (04 12 00 01) or directly with the first data page; subsequent GETs
        yield the remaining pages. Returns pages*64 bytes concatenated.
        """
        frame = _make_frame(OP_READ_COLORS, pages=pages)
        ack = self._send_recv(frame)
        status, _, op = _parse_ack(ack)
        if op == 0:
            # device skipped the ACK and answered data on the first GET
            data = ack
        else:
            data = b""
            t0 = time.time()
            stalls = 0
            while len(data) < pages * 64 and stalls < 24 and time.time() - t0 < 2.0:
                time.sleep(0.02)
                page = self._send_empty()
                if page and page[0] == 0x04 and page[3] == 0x01:
                    # residual ACK frame; fetch the next page instead
                    time.sleep(0.02)
                    page = self._send_empty()
                if not page:
                    stalls += 1
                    continue
                stalls = 0
                data += page
        self.drain()
        if len(data) < pages * 64:
            raise RuntimeError(f"read_colors short read: {len(data)} bytes")
        return data[: pages * 64]

    def write_colors(self, payload: bytes, pages: int = 18):
        """Upload 1152-byte color/effect config (OP_WRITE_COLORS).
           payload must be exactly pages * 64 bytes.
        """
        assert len(payload) == pages * 64, f"Expected {pages*64} bytes, got {len(payload)}"
        frame = _make_frame(OP_WRITE_COLORS, pages=pages)
        ack = self._send_recv(frame)
        _, _, op = _parse_ack(ack)
        if op != OP_WRITE_COLORS:
            raise RuntimeError(f"write_colors unexpected op 0x{op:04x}")
        # send data pages
        for i in range(pages):
            page = payload[i*64:(i+1)*64]
            resp = self._send_recv(page)
        # request checksum
        frame = _make_frame(OP_CHECKSUM, pages=0)
        resp = self._send_recv(frame)
        return _parse_checksum(resp)

    def write_keymap(self, payload: bytes, pages: int = 10):
        """Upload 640-byte key remap table."""
        assert len(payload) == pages * 64
        frame = _make_frame(OP_WRITE_KEYMAP, pages=pages)
        ack = self._send_recv(frame)
        _, _, op = _parse_ack(ack)
        if op != OP_WRITE_KEYMAP:
            raise RuntimeError(f"write_keymap unexpected op 0x{op:04x}")
        for i in range(pages):
            self._send_recv(payload[i*64:(i+1)*64])
        frame = _make_frame(OP_CHECKSUM, pages=0)
        resp = self._send_recv(frame)
        return _parse_checksum(resp)

    def write_macro(self, payload: bytes, pages: int = 1):
        """Upload macro data."""
        frame = _make_frame(OP_WRITE_MACRO, pages=pages)
        ack = self._send_recv(frame)
        _, _, op = _parse_ack(ack)
        if op != OP_WRITE_MACRO:
            raise RuntimeError(f"write_macro unexpected op 0x{op:04x}")
        for i in range(pages):
            self._send_recv(payload[i*64:(i+1)*64])
        frame = _make_frame(OP_CHECKSUM, pages=0)
        resp = self._send_recv(frame)
        return _parse_checksum(resp)

    def read_macro(self, pages: int = 12) -> bytes:
        """Read the macro region back from 0x97C0 (read-side of 0x0415).

        Same framing as the other region reads: after SEND 04 14 the device either ACKs
        (04 14 00 01) or answers straight with the first data page, and
        subsequent GETs yield the rest. Returns pages*64 bytes.

        The region is variable-length -- the OEM app grew it from 512 to 768
        bytes as macros were added -- so `pages` is a window, not a fixed size.

        WARNING: this returns zeros on a C989. Read-back is zero-filled on this
        firmware (verified 2026-09-18, the same reason ReadStateWorker in the
        GUI only trusts non-zero replies), confirmed again for this region
        2026-10-02. The framing below is correct but the device does not fill
        the buffer, so this cannot be used to discover which macros the OEM
        application recorded. Kept because 0x0414 is a real read-op in
        docs/PAYLOAD_OPCODES.md and a future firmware may answer it.
        """
        frame = _make_frame(OP_READ_MACRO, pages=pages)
        data = b""
        ack = self._send_recv(frame)
        status, _, op = _parse_ack(ack)
        if op == 0:
            data = ack          # device skipped the ACK, answered data first
        else:
            while len(data) < pages * 64:
                time.sleep(0.02)
                page = self._send_empty()
                if page and page[0] == 0x04 and page[3] == 0x01:
                    time.sleep(0.02)
                    page = self._send_empty()
                if not page:
                    break
                data += page
        self.drain()
        return data

    def write_gaming(self, enabled: bool):
        """Toggle gaming mode."""
        page = bytearray(REPORT_SIZE)
        page[0] = 0x01 if enabled else 0x00
        frame = _make_frame(OP_WRITE_GAMING, pages=1)
        ack = self._send_recv(frame)
        _, _, op = _parse_ack(ack)
        if op != OP_WRITE_GAMING:
            raise RuntimeError(f"write_gaming unexpected op 0x{op:04x}")
        self._send_recv(bytes(page))
        frame = _make_frame(OP_CHECKSUM, pages=0)
        resp = self._send_recv(frame)
        return _parse_checksum(resp)

    def query_status(self) -> int:
        """Query device status."""
        frame = _make_frame(OP_READ_STATUS, pages=0)
        resp = self._send_recv(frame)
        return resp[3] if len(resp) > 3 else 0xFF

    # ── smart-wheel ring presets (flash 0xA400) ──────────────────────────────
    def read_wheel(self) -> list:
        """Read the 4 wheel-ring preset colors from flash 0xA400.

        Each preset is 4 bytes at 0xA400 + i*4: [function, R, G, B].
        Returns list of (r, g, b) tuples, 4 entries.
        These drive the wheel's own LEDs (daughter board), not the side strips.
        """
        frame = _make_frame(OP_READ_WHEEL, pages=1)
        ack = self._send_recv(frame)
        # no sleep: the data page arrives on the very next empty GET
        page = self._send_empty()
        # if still an ACK, a second GET carries the data
        if len(page) >= 4 and page[0] == 0x04 and page[3] == 0x01:
            time.sleep(0.02)
            page = self._send_empty()
        if len(page) < 16:
            raise RuntimeError(f"read_wheel short payload: {page.hex(' ')}")
        presets = []
        for i in range(4):
            r = page[i*4+1]
            g = page[i*4+2]
            b = page[i*4+3]
            presets.append((r, g, b))
        return presets

    def write_wheel(self, colors) -> int:
        """Write the 4 wheel-ring presets to flash 0xA400.

        colors: list of 4 (r, g, b) tuples. Each preset is [function, R, G, B]
        at 0xA400 + i*4. Page ends with AA 55 marker at 0xA45F.
        Returns reported checksum. Does not affect the side strips.
        """
        assert len(colors) == 4, "need exactly 4 preset colors"
        page = bytearray(REPORT_SIZE)
        for i, (r, g, b) in enumerate(colors):
            page[i*4+0] = i
            page[i*4+1] = r & 0xFF
            page[i*4+2] = g & 0xFF
            page[i*4+3] = b & 0xFF
        page[0x3E] = 0xAA
        page[0x3F] = 0x55
        frame = _make_frame(OP_WRITE_WHEEL, pages=1)
        ack = self._send_recv(frame)
        _, _, op = _parse_ack(ack)
        if op != OP_WRITE_WHEEL:
            raise RuntimeError(f"write_wheel unexpected op 0x{op:04x}")
        self._send_recv(bytes(page))
        frame = _make_frame(OP_CHECKSUM, pages=0)
        resp = self._send_recv(frame)
        return _parse_checksum(resp)

# ── config builder helpers ───────────────────────────────────────────────────
def _pack_effect(e):
    """Pack one effect dict into a 16-byte entry.

    Layout (per Svive README + firmware FUN_000036ba):
        [0]  = ID (mode: 0x00=off, 0x01-0x13=builtin, 0x80=per-key custom)
        [1]  = R
        [2]  = G
        [3]  = B
        [4-7]  = 0 (padding)
        [8]  = Full-color flag (0=solid RGB, nonzero=per-key rainbow)
        [9]  = Brightness (0xFF=max)
        [10] = Speed
        [11] = Direction
        [12-15] = 0x00 0x00 0xAA 0x55 (marker)
    """
    b = bytearray(16)
    b[0]  = e.get("id", 0) & 0xFF
    b[1]  = e.get("r", 0) & 0xFF
    b[2]  = e.get("g", 0) & 0xFF
    b[3]  = e.get("b", 0) & 0xFF
    b[8]  = e.get("full_color", 0) & 0xFF
    b[9]  = e.get("brightness", 0) & 0xFF
    b[10] = e.get("speed", 0) & 0xFF
    b[11] = e.get("direction", 0) & 0xFF
    b[12] = 0x00
    b[13] = 0x00
    b[14] = 0xAA
    b[15] = 0x55
    return b

def build_color_payload(effects=None, key_colors=None, active_effect=0):
    """Build the 1152-byte (18-page) color config payload.

    effects: list of up to 32 effect dicts, each with keys:
        id, r, g, b, brightness, speed, direction, full_color
    key_colors: list of (index, r, g, b) tuples, up to 144 entries
    active_effect: index (0-31) into effects list, or a 16-byte effect entry dict

    Layout (1152 bytes = 18 pages of 64):
        0x000-0x1FF : 32 effect slots × 16 bytes
        0x200-0x43F : 144 key colors × 4 bytes
        0x440-0x44F : active effect block (16 bytes, full copy of one slot)
        0x450-0x47F : padding (48 bytes)
    """
    buf = bytearray(1152)

    if effects:
        for i, e in enumerate(effects[:32]):
            off = i * 16
            buf[off:off + 16] = _pack_effect(e)

    if key_colors:
        for idx, r, g, b in key_colors[:144]:
            off = 0x200 + idx * 4
            buf[off + 0] = 0x80  # 0x80 = key enabled/uses custom color
            buf[off + 1] = r & 0xFF
            buf[off + 2] = g & 0xFF
            buf[off + 3] = b & 0xFF

    # active effect: full 16-byte copy of the chosen effect at 0x440
    if effects and isinstance(active_effect, int) and 0 <= active_effect < len(effects):
        buf[0x440:0x450] = _pack_effect(effects[active_effect])
    elif isinstance(active_effect, dict):
        buf[0x440:0x450] = _pack_effect(active_effect)
    elif effects:
        buf[0x440:0x450] = _pack_effect(effects[0])
    else:
        # fallback: solid red, mode 1
        buf[0x440:0x450] = _pack_effect({"id": 1, "r": 0xFF, "g": 0, "b": 0,
                                          "brightness": 0xFF})

    return bytes(buf)

    def read_keymap(self, pages: int = 10) -> bytes:
        """Read back the uploaded key-remap table (0x90C0, read-side of 0x0411).

        Response framing mirrors read_colors / read_info (docs/PAYLOAD_OPCODES.md):
        after SEND 04 11 the device either ACKs (04 11 00 01) or answers
        straight with the first data page; subsequent GETs yield the remaining
        pages. Returns pages*64 bytes concatenated (entry i at page i//10*64 +
        i%10*4 territory as per the write layout in cloudnine_keymap)."""
        frame = _make_frame(OP_READ_KEYMAP, pages=pages)
        data = b""
        ack = self._send_recv(frame)
        status, _, op = _parse_ack(ack)
        if op == 0:
            data = ack          # device skipped the ACK, answered data first
        else:
            while len(data) < pages * 64:
                time.sleep(0.02)
                page = self._send_empty()
                if page and page[0] == 0x04 and page[3] == 0x01:
                    time.sleep(0.02)
                    page = self._send_empty()
                if not page:
                    break
                data += page
        self.drain()
        return data


    def read_lighting_at(self, keyoff: int = 0):
        """Read back the per-key lighting table (region 0x9340, same region the
        OEM app fills when you assign a color to a key). `keyoff` = key index
        (0-based, 4 bytes each). Mirrors read_colors' paging: GET until ACK
        consumed + pages drained."""
        pages = 10  # 0x9340 is 10 pages (640B); see PAYLOAD_OPCODES / GUI note
        frame = _make_frame(OP_READ_COLORS, pages=pages)
        ack, _ = self._send_recv(frame) if False else (b"", self._send_recv(frame))
        data = bytearray()
        got = 0
        while got < pages * 64:
            frame = _make_frame(OP_READ_COLORS, pages=pages, seq=got)
            page = self._send_recv(frame)
            if page.startswith(b"\x04\x12\x00\x01"):   # ACK consumed
                continue
            data += page
            got += len(page)
        return bytes(data[keyoff*4:(keyoff+1)*4])

