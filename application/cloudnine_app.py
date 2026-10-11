"""
Cloudnine ErgoFSK C989 backlight control app.

Replicates the OEM DeviceDriver.exe wire protocol captured under Wine:

    SEND 04 18            (transfer lead: bit6 flag toggle) -> ack 04 18 00 01
    SEND 04 13 [8]=0x12    (start upload, 18 pages)        -> ack 04 13 00 01
    SEND 18 x data pages  (no ack between pages)
    SEND 04 02             (request checksum)              -> ack 04 02 00 01 <sum lo> <sum hi>
    SEND 04 f0             (commit/apply)                  -> ack 04 f0 00 01

On THIS board the 0x0402 reply is the acceptance signal: sessions returning
`04 02 00 01 <sum>` (matching) are the only ones ever observed to persist
across a power cycle. `04 02 00 ff`/`00` mean the session was NOT accepted;
the board can refuse 0x0413 for hours at a time, and the OEM app gets `ff`
replies too. The 0x04F0 commit is still sent in commit mode regardless,
matching the OEM wire capture byte-for-byte, but on this board it is a no-op
on refused sessions (it echoes `04 f0 00 00` and nothing persists), so the
0x04F0 round-trip and its ack byte carry no persistence signal — the outcome
is judged on the 0x0402 reply (see apply_region and checksum_summary). A
refused session is re-issued a few times before the apply is called a failure
(REFUSAL_RETRIES); refusals are the board's intermittent state, not a host
fault.

Payload layout (1152 bytes = 18 pages):
    0x000-0x13F  effect slots: 20 x 16 bytes (slot i has mode id i+1)
                    slot: [0]=mode id [1..3]=RGB [4..7]=0 [8]=fullcolor
                          [9]=[10]=06 [11..13]=0 [14..15]=AA 55
    0x140-0x1F7  zero padding
    0x1F8-0x43F  per-key colors: 146 x 4 bytes = [0x80,R,G,B]
    0x440-0x44F  active-effect block (16-byte copy of one slot)
    0x450-0x47F  zero padding

Checksum = sum(all 1152 bytes) & 0xFFFF (verified against firmware ACK 0x88ba).

The per-key region is 146 entries, not 144, and starts at 0x1F8 rather than
0x200. Entries 144/145 carry the two side-light channels (146, 147) that no
pre-strip28 image could reach; see docs/KEYMAP.md. firmware.strip28.bin
reads the region from 0x1F8 (146 entries end exactly at 0x43F, still clear of
the active block at 0x440). Every EARLIER firmware -- firmware.bin.m10 and
firmware.renderfix.bin included -- reads it from 0x200 for 144 entries, so
writing this layout to one of those lights every key with its neighbour's
colour. Check which image is flashed before using this module.
"""
import sys
import time
import struct
import os
import datetime

from sonix_kbd import Keyboard, REPORT_SIZE, FRAME_SIZE, ACK_DELAY_S

TEMPLATE = b""  # filled from DEFAULT_CONFIG

DEFAULT_CONFIG = "/tmp/default_config.bin"

# ── asset paths ──────────────────────────────────────────────────────────────
# The app runs from two layouts: the repo root, with data/ and settings/ beside
# cloudnine_gui.py, and the packaged copy under Upload/application/, where they
# are NOT beside it. Resolving assets against __file__ alone silently found
# nothing in the second layout, and the symptom was not an error -- it was a
# *blank* wheel page, because the caller substituted zeros (see load_default in
# cloudnine_gui.py). Search the plausible roots instead.
HERE = os.path.dirname(os.path.abspath(__file__))
ASSET_ROOTS = (
    HERE,                                       # repo root, or Upload/application
    os.path.dirname(HERE),                      # Upload/  (packaged copy)
    os.path.dirname(os.path.dirname(HERE)),     # repo root, from Upload/application
)


def asset_path(*parts, required=True):
    """Absolute path to a bundled data/ or settings/ file.

    `required=False` returns the first candidate even if it does not exist, for
    callers that want to test for the file themselves.
    """
    for root in ASSET_ROOTS:
        path = os.path.join(root, *parts)
        if os.path.exists(path):
            return path
    if required:
        raise FileNotFoundError(
            f"missing bundled asset {'/'.join(parts)}; looked in: "
            + ", ".join(ASSET_ROOTS)
            + ". Copy data/ and settings/ next to the app (or one level up).")
    return os.path.join(HERE, *parts)


# ── firmware asset paths ─────────────────────────────────────────────────────
# flash.sh, sonixflasher and the firmware images live in settings/FWFiles/ in
# the repo. The packaged release (tools/package_upload.py) keeps them in a
# single Upload/firmware/ folder instead, because they are what the user
# flashes and the release deliberately omits the diagnostic images that sit
# beside them here. This is the one place that difference is absorbed:
# firmware_path() searches both layouts, so the GUI's flash buttons work from
# either without a duplicate settings/ tree under Upload/application/.
FIRMWARE_ROOTS = (
    os.path.join(HERE, "settings", "FWFiles"),
    os.path.join(HERE, "firmware"),
    os.path.join(os.path.dirname(HERE), "settings", "FWFiles"),
    os.path.join(os.path.dirname(HERE), "firmware"),
)


def firmware_path(name, required=True):
    """Absolute path to a flash asset (flash.sh, sonixflasher, a firmware image).

    Prefers settings/FWFiles/ (repo layout), then firmware/ (release layout), at
    HERE and its parent. The grandparent is deliberately not searched: from
    Upload/application it resolves to the repo root, and pulling the repo's
    firmware.strip28m10.bin diagnostic image into a release run is exactly the
    leak this resolver exists to avoid.

    `required=False` returns the first candidate even if it does not exist, for
    callers that want to test the file themselves with os.path.isfile.
    """
    for root in FIRMWARE_ROOTS:
        path = os.path.join(root, name)
        if os.path.exists(path):
            return path
    if required:
        raise FileNotFoundError(
            f"missing firmware asset {name}; looked in: "
            + ", ".join(FIRMWARE_ROOTS))
    return os.path.join(FIRMWARE_ROOTS[0], name)


EFFECT_REGION = 0x000
KEY_COUNT = 146
ACTIVE_BLOCK = 0x440
# Derived, not literal: 146 entries must end exactly at ACTIVE_BLOCK so the
# last one (145, side-light channel 147) cannot spill into the active block.
KEY_REGION = ACTIVE_BLOCK - KEY_COUNT * 4      # 0x1F8

# Effect id 0x80 is the per-key mode. It is the one id where the active block
# does NOT decide what the board shows: the board renders the per-key region
# instead and the active block's colour is ignored. That is why _describe has to
# report the per-key region whenever it sees this id -- a payload can carry a
# black active block and still paint the whole board orange, and reading only
# the active block makes that look like a bug in the log.
USER_ID = 0x80

# The board intermittently refuses an upload session: at the 0x0402 acceptance
# query it answers `04 02 00 ff` (or `00`) instead of `04 02 00 01 <sum>`. It is
# not a transport fault and not something the host sends wrong -- the OEM app's
# own capture gets `ff` replies too (data/oem_capture_apply.log session 1) -- and
# on this board it usually clears within seconds (05:20:34 refused, 05:20:38
# accepted; observed 2026-10-01). So a refused session is re-issued a few times,
# with a short flat pause, before we give up. This budget is deliberately short:
# a genuine multi-minute reject window (docs/investigation.md) is not worth
# holding the GUI on, and the caller is told to replug instead. Transport faults
# (a 0x04F0 round-trip that does not echo) keep their separate `retry_delay`
# schedule.
REFUSAL_RETRIES = 5
REFUSAL_DELAY_S = 0.5

# The session-opening opcode apply_region sends by default. 0x0418 clears bit 6
# of DAT_20000172 and 0x0419 sets it; both are the same "flag toggle" lead and
# every colors/keymap/gaming/wheel-ring session in the OEM captures uses 0x0418.
# The macro region is the one exception -- see apply_macro.
OP_LEAD_DEFAULT = 0x0418
OP_LEAD_MACRO = 0x0419


def default_template():
    """Reconstruct the OEM factory config (1152 B) when the captured file is
    unavailable. Slots 1..19 = ids 0x01..0x13 (bri/spd 6; slots >= 7 use the
    full-spectrum palette), slot 20 = 0x80; per-key region zeroed."""
    buf = bytearray(18 * 64)
    for i in range(20):
        o = i * 16
        buf[o] = (0x80 if i == 19 else i + 1)
        buf[o + 8] = 1 if i >= 6 else 0
        buf[o + 9] = 0x06
        buf[o + 10] = 0x06
        buf[o + 14] = 0xAA
        buf[o + 15] = 0x55
    return bytes(buf)


def load_template():
    try:
        data = open(DEFAULT_CONFIG, "rb").read()
        if len(data) == 18 * 64:
            return data
    except OSError:
        pass
    return default_template()


def build_payload(mode=1, r=0, g=0, b=0, fullcolor=0, template=None,
                  key_override=0xFF):
    """Return the 1152-byte payload for a given active effect.

    mode:       effect/mode id (1..19). The active-effect block gets the mode
                and RGB. The matching slot in the effects region is updated too.
    key_override: 0..255 flag byte written into every key-(R,G,B) pair that is
                enabled (0x80). Set to preserve whatever is in the template.
    """
    if template is None:
        template = load_template()
    buf = bytearray(template)

    slot = bytearray(16)
    slot[0] = mode & 0xFF
    slot[1] = r & 0xFF
    slot[2] = g & 0xFF
    slot[3] = b & 0xFF
    slot[8] = fullcolor & 0xFF
    slot[9] = 0x06
    slot[10] = 0x06
    slot[14] = 0xAA
    slot[15] = 0x55

    if 1 <= mode <= 20:
        buf[(mode - 1) * 16 : (mode - 1) * 16 + 16] = slot
    buf[ACTIVE_BLOCK : ACTIVE_BLOCK + 16] = slot

    if key_override is not None:
        for off in range(KEY_REGION, ACTIVE_BLOCK, 4):
            if buf[off] == 0x80:
                buf[off] = key_override & 0xFF
    return bytes(buf)


def checksum(payload: bytes) -> int:
    return sum(payload) & 0xFFFF


LOG_PATH = os.path.join(os.path.expanduser("~"), ".config", "cloudnine-c989",
                        "device_responses.log")


def _log(msg):
    """Timestamp one line to the response log and to stdout.

    The GUI has no print() of its own, so stdout is the only way these reach
    /tmp/cn_live.log. Set CLOUDNINE_LOG=0 to silence both sinks.
    """
    if os.environ.get("CLOUDNINE_LOG") == "0":
        return
    line = f"{datetime.datetime.now():%H:%M:%S.%f} {msg}"
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001  - never let logging break a write
        pass
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _describe_perkey(payload):
    """Summarise the per-key region, which is what paints the board in 0x80.

    Each of the 146 entries is [0x80,R,G,B]; an entry whose first byte is not
    0x80 is not painted, and a 000000 entry is indistinguishable from an absent
    one on the board (see LightState._key_color). So the summary counts only
    real, lit entries and names the colour they carry.
    """
    lit = {}
    for i in range(KEY_COUNT):
        o = KEY_REGION + i * 4
        entry = payload[o:o + 4]
        if len(entry) < 4 or entry[0] != 0x80:
            continue
        rgb = tuple(entry[1:])
        if not any(rgb):
            continue
        lit[rgb] = lit.get(rgb, 0) + 1
    off = KEY_COUNT - sum(lit.values())
    if not lit:
        return f"per-key: nothing painted ({KEY_COUNT} keys off)"
    parts = ", ".join(f"#{r:02x}{g:02x}{b:02x} x{n}"
                      for (r, g, b), n in sorted(lit.items(), key=lambda kv: -kv[1]))
    tail = f", {off} off" if off else ""
    return f"per-key: {parts}{tail}"


def _describe(payload):
    """One-line summary of a color/config payload, for the log.

    The active block decides what the board does, with ONE exception: at effect
    id 0x80 the board renders the per-key region and ignores the active colour.
    That exception is why both are reported below. A payload can carry a black
    active block and still paint the whole board orange -- which is exactly what
    happened on 2026-09-30, and reading only the active block made the log look
    broken when it was the truth.

    Byte [9] of the active block is a firmware brightness *index* rather than a
    0-255 level; logging it raw is what makes a bad value obvious after the fact.
    """
    if len(payload) < ACTIVE_BLOCK + 16:
        return ""
    a = payload[ACTIVE_BLOCK:ACTIVE_BLOCK + 16]
    head = (f"active: id=0x{a[0]:02x} rgb=#{a[1]:02x}{a[2]:02x}{a[3]:02x} "
            f"fullcolor={a[8]} bri={a[9]} (0x{a[9]:02x}) spd={a[10]} dir={a[11]}")
    if a[0] == USER_ID:
        # Say out loud that the colour above is not the one the board is using,
        # otherwise the two lines contradict each other on the same log entry.
        return (f"{head}  <- ignored in this mode; {USER_ID:#04x} paints per-key"
                f" | {_describe_perkey(payload)}")
    return head


def apply_region(kb, opcode: int, payload: bytes, delay=0.02, commit=True,
                 attempts=25, retry_delay=0.4,
                 refusal_retries=REFUSAL_RETRIES,
                 refusal_delay=REFUSAL_DELAY_S,
                 lead=OP_LEAD_DEFAULT):
    """Send the exact OEM upload sequence for an arbitrary config region.

    `opcode` is the region's write header (e.g. 0x0413 colors, 0x0425
    wheel ring A, 0x0427 wheel ring B); `payload` must be a whole number of
    64-byte pages. Each region is uploaded in its OWN session
    (lead -> header -> pages -> checksum -> commit), exactly like the OEM app.

    `lead` is the session-opening opcode, which is NOT the same for every
    region. 0x0418 (bit6 clear) opens the colors/keymap/gaming/wheel-ring
    sessions; the OEM app opens macro-region sessions with 0x0419 (bit6 set)
    instead -- data/oem_playmacro_capture.log is `0419 -> 0415 -> 0402` on
    every macro write, never 0x0418. Both are the same "flag toggle" on
    DAT_20000172, so this is not guesswork about which is right: the capture
    says which one the firmware is addressed with for this region.

    The 0x0402 checksum query is the ACCEPTANCE signal on this board. Its
    replies are `04 02 00 01 <sum>` (accepted; a checksum is reported),
    `04 02 00 ff` and the device-specific `04 02 00 00` (session NOT accepted;
    no checksum). Only sessions that returned `04 02 00 01 <sum>` matching the
    host have ever been observed to persist across a power cycle (e.g. the
    07:18 commit, docs/investigation.md). The board can refuse 0x0413 for
    hours at a time — "reject window" states that retrying does not
    short-circuit.

    We still send the 0x04F0 commit in commit mode regardless of the 0x0402
    reply, matching the OEM capture byte-for-byte (the OEM closes with 0x04F0
    even after `04 02 00 ff` — data/oem_capture_apply.log session 1). But on
    this board 0x04F0 is a no-op on refused sessions (it echoes
    `04 f0 00 00` and nothing persists); the OEM's `04 f0 00 01` acks are its
    own firmware executing the write. The 0x04F0 round-trip and ack byte carry
    NO persistence signal here: 2026-09-29 16:23 validation sent the commit
    after four `ff`/`00` replies, got four `04 f0 00 00` echoes, and none of
    the writes survived a replug. Outcome is therefore judged on the 0x0402
    reply, and checksum_summary() returns ok=False when no checksum was
    reported. Retries cover transport/session breakage only.

    `attempts` bounds transport-failure retries (a 0x04F0 round-trip that does
    not echo). `retry_delay` is the pause between them and may be a callable
    taking the attempt index.

    A *refusal* -- the 0x04F0 round-trip echoes but 0x0402 answered
    `04 02 00 ff`/`00` -- is re-issued too, on its own budget:
    `refusal_retries` more sessions, pausing `refusal_delay` between them. That
    budget is short on purpose. The refusal is the board's intermittent reject
    state (the OEM app's capture gets `ff` replies too,
    data/oem_capture_apply.log session 1) and it usually clears within seconds,
    so a handful of retries catches the common case; a genuine multi-minute
    reject window (docs/investigation.md) is not worth blocking the GUI on, and
    the caller is told to replug instead.

    Returns (checksum_reported, checksum_expected); `checksum_reported` is
    None when the device did not report one (`04 02 00 ff`/`00`) -- the session
    was refused on every attempt, which in commit mode is a FAILURE (the commit
    opcode cannot persist an unaccepted upload on this board)."""
    assert len(payload) % 64 == 0 and len(payload) > 0
    pages = len(payload) // 64

    # No kb.drain() here, on purpose. It used to spray 64 empty feature reports
    # before every session, on the theory that unconsumed read pages block new
    # sessions. DeviceDriver.exe never drains, so this is OEM-faithful and it
    # stays gone.
    #
    # It is worth recording that neither the drain nor the read-back was what
    # was wrong. See docs/investigation.md. A 0x00 at payload offset 3 means
    # our own byte came back unpatched, and it was being read too early to see
    # the patch (sonix_kbd.ACK_DELAY_S, which is the actual fix). In the 949
    # applies in device_responses.log, 72 of the 75 the device accepted came
    # back with a 0x00 header -- so that byte never tracked acceptance either
    # way, and nothing about it should be read as "the device refused".
    #
    # The acceptance test that has actually tracked it is the 0x0402 check
    # below, reinstated in e44b12a. It is the only one.

    def cmd(op, n=0):
        f = bytearray(REPORT_SIZE)
        f[0] = (op >> 8) & 0xFF
        f[1] = op & 0xFF
        f[8] = n & 0xFF
        f[9] = (n >> 8) & 0xFF
        return bytes(f)

    expected = checksum(payload)
    last = ""
    refusals = 0
    desc = _describe(payload)
    for attempt in range(attempts):
        _log(f"write 0x{opcode:04x}: {pages} pages, {len(payload)} B, "
             f"checksum 0x{expected:04x}, ack delay {ACK_DELAY_S * 1000:.0f}ms"
             f"{' | ' + desc if desc else ''}"
             f"{' (attempt %d)' % (attempt + 1) if attempt else ''}")
        # 1. transfer lead (stateful; opens/arms a session)
        r = kb._send_recv(cmd(lead))
        _log(f"  -> {lead:04x} ack {r[:8].hex(' ')}")

        # 2. upload start header
        r = kb._send_recv(cmd(opcode, pages))
        _log(f"  -> {opcode:04x} ack {r[:8].hex(' ')}")

        # 3. data pages (SFEATURE only, no read-back — matches OEM app)
        for i in range(pages):
            kb._send_only(payload[i * 64 : (i + 1) * 64])
            time.sleep(delay)
        _log(f"  -> {pages} data pages sent")

        # 4. checksum request — the ACCEPTANCE signal on this board.
        #    `04 02 00 01 <sum>` = accepted, checksum reported; `04 02 00 ff`
        #    and `04 02 00 00` = session not accepted. Only accepted sessions
        #    have ever persisted for us. The 0x04F0 works only on accepted
        #    sessions (see the docstring: on this board it is a no-op echo
        #    on refused ones), so reported=None below means FAILURE.
        reported = None
        resp = kb._send_recv(cmd(0x0402))
        if resp[:3] == bytes.fromhex("04 02 00") and resp[3] == 0x01:
            reported = struct.unpack_from("<H", resp, 4)[0]
            _log(f"  -> 0402 ack {resp[:8].hex(' ')} = 0x{reported:04x} "
                 f"({'MATCH' if reported == expected else 'MISMATCH'})")
        else:
            _log(f"  -> 0402 reply {resp[:8].hex(' ')} "
                 f"(session not accepted)")

        if not commit:
            return reported, expected

        # 5. commit / apply — sent regardless of the 0x0402 reply to mirror
        #    the OEM capture byte-for-byte. On this board it only persists an
        #    ACCEPTED session; on a refused one it echoes `04 f0 00 00` and
        #    does nothing (no persistence signal in the round-trip or ack).
        #    The round-trip failing to echo `04 f0` at all is transport
        #    breakage and retries the whole session on the transport budget.
        r = kb._send_recv(cmd(0x04F0))
        _log(f"  -> 04f0 ack {r[:8].hex(' ')}")
        if r[:2] != bytes.fromhex("04 f0"):
            last = r[:8].hex(" ")
            _log(f"  -> 04f0 round-trip failed {last}, retrying session")
            time.sleep(retry_delay(attempt) if callable(retry_delay) else retry_delay)
            continue
        # Round-trip OK. An accepted session is the only good outcome.
        if reported is not None:
            return reported, expected
        # The board refused the session (`04 02 00 ff`/`00`). That is its
        # intermittent reject state, not a transport fault, so re-issue it on
        # the short refusal budget. `last` is cleared because the 0x04F0
        # round-trip itself was fine; it only records transport breakage.
        last = ""
        refusals += 1
        if refusals > refusal_retries:
            _log(f"  -> session refused {refusals} time(s); giving up")
            return None, expected
        pause = refusal_delay(attempt) if callable(refusal_delay) else refusal_delay
        _log(f"  -> session refused ({resp[:8].hex(' ')}); retrying in "
             f"{pause:.1f}s ({refusals}/{refusal_retries})")
        time.sleep(pause)
    if last == "":
        # Every session was refused; the transport was healthy throughout.
        return None, expected
    raise RuntimeError(
        f"region 0x{opcode:04x} failed after {attempts} attempts; "
        f"last 04f0 ack: {last}. The device is not echoing 0x04F0 -- this "
        f"is the state that a power-cycle clears. Unplug and replug it, do "
        f"not press a key, then apply again.")


def checksum_summary(reported, expected, commit):
    """Human summary for the apply_region return value.

    `reported is None` means the device answered `04 02 00 ff`/`00` — the
    session was NOT accepted. From 2026-09-29 16:23 on-device validation:
    sending 0x04F0 through such a reply echoes `04 f0 00 00` and persists
    nothing, so in commit mode this is a FAILURE, never a success.

    Returns (ok, message)."""
    if reported is None:
        if commit:
            return False, ("device did not accept the session "
                           "(\"04 02 00 ff/00\", no checksum reported); "
                           "the 0x04F0 commit cannot persist an unaccepted "
                           "upload on this board")
        return False, "staged; the device did not accept the session (unverified)"
    ok = reported == expected
    if ok:
        return True, f"0x0402 checksum 0x{reported:04x} MATCH"
    return False, (f"0x0402 checksum 0x{reported:04x} MISMATCH "
                   f"(expected 0x{expected:04x})")


def apply_keymap(kb, payload: bytes, delay=0.02, commit=True, attempts=25,
                 retry_delay=0.4, refusal_retries=REFUSAL_RETRIES,
                 refusal_delay=REFUSAL_DELAY_S):
    """Upload the 640-byte Top keymap layer (0x0411, flash 0x90C0). See
    apply_region.

    `payload` must be the full 10-page (640-byte) keymap from
    cloudnine_keymap.build_payload()."""
    assert len(payload) == 10 * 64
    return apply_region(kb, 0x0411, payload, delay, commit, attempts,
                        retry_delay, refusal_retries, refusal_delay)


def apply_fn_layer(kb, payload: bytes, delay=0.02, commit=False, attempts=25,
                   retry_delay=0.4, refusal_retries=REFUSAL_RETRIES,
                   refusal_delay=REFUSAL_DELAY_S):
    """Upload the 640-byte Fn keymap layer (0x0427, flash 0xA440). See
    apply_region.

    **Seen on the wire 2026-10-03.** data/oem_reset_capture.log is the OEM app
    sending this region for real: `0418 -> 0427 -> 10 pages -> 0402 -> 04F0`,
    i.e. the ordinary 0x0418 lead and the ordinary 0x04F0 commit, unlike the
    macro region's 0x0419 lead and no commit. That capture also yielded the
    layer's factory contents -- already held as data/oem_defaults/wheel_b.bin
    and now confirmed against this upload -- which the Fn layer needs as its
    own default, because it ships 94 of its 160 keys disabled and clearing it
    to the Top layer's keymap re-enabled every one of them.

    `commit` still defaults to False here, and the GUI still stages this layer,
    because nothing has yet confirmed that a layer staged this way survives a
    power cycle. Pass commit=True to write it for real.
    """
    assert len(payload) == 10 * 64
    return apply_region(kb, 0x0427, payload, delay, commit, attempts,
                        retry_delay, refusal_retries, refusal_delay)


def apply_config(kb, payload: bytes, delay=0.02, commit=True, attempts=25,
                 retry_delay=0.4, refusal_retries=REFUSAL_RETRIES,
                 refusal_delay=REFUSAL_DELAY_S):
    """Upload the 1152-byte color/effect region (0x0413). See apply_region."""
    assert len(payload) == 18 * 64
    return apply_region(kb, 0x0413, payload, delay, commit, attempts,
                        retry_delay, refusal_retries, refusal_delay)


def apply_macro(kb, payload: bytes, delay=0.02, attempts=25,
                retry_delay=0.4, refusal_retries=REFUSAL_RETRIES,
                refusal_delay=REFUSAL_DELAY_S):
    """Upload the macro region (0x0415). See apply_region.

    Two things differ from every other region, both taken from the OEM capture
    rather than assumed:

    * the session is opened with lead 0x0419, not the usual 0x0418
      (data/oem_playmacro_capture.log shows `0419 -> 0415 -> 0402` on every
      macro write);
    * **no commit is sent**. Every macro session in that capture ends at the
      0x0402 checksum. The keymap sessions that do carry a 0x04F0 commit are a
      different shape, and adding one here would be sending a byte the OEM
      never sends to this region.

    There is deliberately no `commit` argument: the OEM never commits this
    region, so neither can we.

    **A macro write replaces the WHOLE region.** The device's macro slots are
    addressed by the pointer table at the head of the region, not updated in
    place, so a region carrying fewer macros has fewer slots and the ones that
    were dropped are gone. The OEM app's recorded macros cannot be read back
    (0x0414 returns zeros on this firmware), so there is no way to re-send
    them: writing from here is a one-way door for macros the OEM app recorded.
    Callers must make the user aware of this before writing.
    """
    assert len(payload) % 64 == 0 and len(payload) > 0
    return apply_region(kb, 0x0415, payload, delay, False, attempts,
                        retry_delay, refusal_retries, refusal_delay,
                        lead=OP_LEAD_MACRO)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Cloudnine C989 backlight control")
    ap.add_argument("--mode", type=lambda x: int(x, 0), default=1)
    ap.add_argument("--rgb", default="FF0000", help="RRGGBB hex")
    ap.add_argument("--fullcolor", type=int, default=0)
    ap.add_argument("--verify", action="store_true",
                    help="byte-identical replay of the captured default config")
    args = ap.parse_args()

    if args.verify:
        payload = open(DEFAULT_CONFIG, "rb").read()
    else:
        rgb = args.rgb.lstrip("#")
        r, g, b = (int(rgb[i:i + 2], 16) for i in (0, 2, 4))
        payload = build_payload(args.mode, r, g, b, args.fullcolor)

    print(f"checksum = 0x{checksum(payload):04x}")
    kb = Keyboard()
    try:
        reported, expected = apply_config(kb, payload)
        ok, msg = checksum_summary(reported, expected, commit=True)
        print(f"firmware {msg}")
        print("applied" if ok else "FAILED")
        return 0 if ok else 1
    finally:
        kb.close()


if __name__ == "__main__":
    main()