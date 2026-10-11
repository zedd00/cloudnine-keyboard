#!/usr/bin/env python3
"""Cloudnine ErgoFSK C989 — key remap payload model.

Canonical remap encoding (proven on-device, see docs/KEYMAP.md):

    entry[source_key_index] = 02 00 <HID usage of TARGET> 00

* 0x0411 region, 640 bytes = 10 pages = 160 entries of 4 bytes.
* Entry index == key_index of the SOURCE key (from KeyboardLayout.xml).
* byte2 == raw HID keyboard usage code of the TARGET (mode-independent),
  e.g. D=0x07, F=0x09, G=0x0a, H=0x0b, J=0x0d, F5=0x3e.
* `00 00 00 00` == no remap (key passes through).
* Entry 159 always holds the sentinel `00 00 aa 55`.
* Light (89) and Fn (72) are not reachable via the OEM app; both were verified
  via direct write. Fn (72) remaps on the keymap alone; Light (89) needs the
  firmware M10 patch first to remove the 0xd3 lighting intercept (see
  firmware_light_patch_ok). Our v1.0 image sets 0x622f = 0x74 (M10 emits
  nothing until a keymap entry is written for idx 89); the OEM image keeps the
  stock 0xd3 intercept.

Targets are expressed as HID usages, so any standard HID keyboard value is
allowed independently of what physical keys exist on the C989.
"""
import os
import re

from cloudnine_app import asset_path, firmware_path

HERE = os.path.dirname(os.path.abspath(__file__))
XML = asset_path("software-exe-extract", "Cloud Nine Keyboard Application",
                 "KeyboardLayout.xml", required=False)

KEYMAP_SIZE = 640          # bytes at 0x0411
ENTRIES = KEYMAP_SIZE // 4 # 160 × 4-byte action words
SENTINEL_IDX = 159
SENTINEL = b"\x00\x00\xaa\x55"
REMAP = b"\x02\x00"        # prefix of a remap action word

# Keys the OEM app cannot remap (see docs/KEYMAP.md). Excluded until verified.
# Both have now been direct-write verified: Fn (72) remaps fine; Light (89)
# only works AFTER the M10 firmware patch removes the lighting intercept.
UNMAPPABLE = set()

# Light (89) becomes a normal key ONLY after the firmware M10 patch (see
# docs/KEYMAP.md + docs/M10_FIX_FLASH.md + docs/M10_LIGHTFLAG_LOG.md). The
# lighting intercept is keyed on the firmware HID-code table: M10's scan
# position (col 1, row 13, key_index 89) carries base code 0xd3 at flash
# 0x622f, and FUN_00002264:0x22b2 swallows every keystroke whose code == 0xd3
# into the lighting color-cycle (before keymap / host report). Our v1.0 image
# sets that byte to 0x74 (inert), so M10 neither intercepts nor types anything
# until it is assigned. (The earlier 0x45b and 0xd7cc "hijack" theories were
# disproven on-device.)
LIGHT_IDX = 89
LIGHT_CODE_OFFSET = 0x622f          # M10 base HID code slot in the 0x6222 table
LIGHT_CODE_UNPATCHED = 0xd3         # "Light" usage: swallowed -> lighting cycle
LIGHT_CODE_PATCHED = 0x0d           # J: normal key (passes the >=0x78 drop gate)
LIGHT_CODE_INERT = 0x74             # inert until assigned (0x70-0x79 early return)
# The two product images the GUI ships and flashes. Flash assets live in
# settings/FWFiles/ in the repo; the packaged release (tools/package_upload.py)
# keeps them in Upload/firmware/. firmware_path() searches both, so the GUI's
# flash buttons work from either layout. The repo also holds the diagnostic /
# intermediate builds (strip28, the M10->J and inert variants, ...) -- those
# stay for history and the build chain but are NOT shipped and NOT surfaced in
# the GUI.
OEM_FW = firmware_path("firmware.oem.bin", required=False)  # untouched factory image
V1_FW = firmware_path("firmware.v1.0.bin", required=False)  # ours: side lights + inert M10 + ring


def _reference_image():
    """The image the GUI's M10 firmware status should describe.

    That is the v1.0 product image (0x74, inert M10). If it is somehow absent,
    fall back to the OEM image so the status still reads a real byte rather than
    a bogus missing-file default.
    """
    for path in (V1_FW, OEM_FW):
        if path and os.path.isfile(path):
            return path
    return V1_FW


def firmware_light_code(path=None):
    """Return the M10 base HID code byte in a firmware image, or None.

    Lets callers distinguish the three states (stock 0xd3 / J 0x0d / inert 0x74)
    instead of collapsing them into a bool. `path=None` inspects the shipped
    reference image (_reference_image)."""
    if path is None:
        path = _reference_image()
    try:
        with open(path, "rb") as f:
            f.seek(LIGHT_CODE_OFFSET)
            b = f.read(1)
        return b[0] if len(b) == 1 else None
    except OSError:
        return None


def firmware_light_patch_ok(path=None) -> bool:
    """True if the firmware image avoids the 0xd3 intercept (byte 0x622f != 0xd3).

    `path=None` inspects the shipped reference image (_reference_image).

    Any image whose M10 slot is not 0xd3 lets M10 through to the keymap/report
    path. Two patched values are recognised:

    - 0x0d (J)  -- diagnostic build firmware.strip28m10.bin: M10 types J
    - 0x74 (inert) -- our v1.0 image (firmware.v1.0.bin): M10 produces nothing
      until a keymap entry is written for idx 89

    The intercept is keyed on code 0xd3, so any image whose M10 slot still reads
    0xd3 keeps swallowing M10 presses into the lighting color-cycle; that is what
    made every keymap remap of idx 89 fail before the firmware fix."""
    if path is None:
        path = _reference_image()
    try:
        with open(path, "rb") as f:
            f.seek(LIGHT_CODE_OFFSET)
            return f.read(1) in (bytes([LIGHT_CODE_PATCHED]),
                                 bytes([LIGHT_CODE_INERT]))
    except OSError:
        return False


# Each <key .../> element, then its attributes.
#
# The element body must tolerate a ">" INSIDE a quoted value: the C989's period
# key is <key code="0x37" name=">" desc=">" .../>, and a plain [^>]*? stops at
# the legend and silently drops the key. The inner group allows any character
# except > and ", or a complete quoted string, so legends like ">" and "\|"
# both parse. Attribute order is not assumed.
_KEY_EL = re.compile(r'<key\s+((?:[^>"]|"[^"]*")*?)/?>')
_ATTR = re.compile(r'([A-Za-z_]+)\s*=\s*"([^"]*)"')


def _load_xml():
    """Return {key_index: (hid_usage, name)} from the LayoutK89M block."""
    out = {}
    try:
        seg = open(XML, encoding="utf-8").read()
        seg = seg.split("<LayoutK89M")[1].split("</LayoutK89M>")[0]
    except (OSError, IndexError, UnicodeDecodeError):
        return out
    for el in _KEY_EL.finditer(seg):
        a = dict(_ATTR.findall(el.group(1)))
        if "key_index" not in a or "code" not in a:
            continue
        try:
            out[int(a["key_index"])] = (int(a["code"], 16), a.get("name", ""))
        except ValueError:
            continue
    return out


KEYS = _load_xml()   # key_index -> (hid_usage, name)


def name(idx):
    return KEYS.get(idx, ("", f"idx {idx}"))[1]


def hid_usage(idx):
    return KEYS.get(idx, (None,))[0]


def default_payload():
    """640-byte all-zero keymap with the tail sentinel."""
    buf = bytearray(KEYMAP_SIZE)
    buf[SENTINEL_IDX * 4:SENTINEL_IDX * 4 + 4] = SENTINEL
    return bytes(buf)


def build_payload(remaps: dict) -> bytes:
    """Build the 640-byte 0x0411 payload.

    remaps: {source_key_index: target} where target is the **HID usage code**
      of the destination key (int), or None/0/omitted => no remap (zero entry).
      Passing a key_index in the value would collide with HID usages, so use
      `build_payload_from_keys()` when targets are given as key indices.
    """
    buf = bytearray(default_payload())
    for idx, target in remaps.items():
        if idx is None or idx < 0 or idx >= SENTINEL_IDX:
            continue
        if idx in UNMAPPABLE:
            continue
        if not target:
            buf[idx * 4:idx * 4 + 4] = b"\x00\x00\x00\x00"
            continue
        usage = int(target, 0) if isinstance(target, str) else int(target)
        buf[idx * 4:idx * 4 + 4] = bytes([0x02, 0x00, usage & 0xFF, 0x00])
    return bytes(buf)


def build_payload_from_keys(remaps: dict) -> bytes:
    """Same as build_payload but `remaps` values are TARGET key indices
    (resolved to their HID usage via KEYS), e.g. {56: 78} remaps A to B."""
    resolved = {}
    for idx, tgt in remaps.items():
        if tgt and tgt in KEYS:
            resolved[idx] = KEYS[tgt][0]
        else:
            resolved[idx] = tgt
    return build_payload(resolved)


# B1 of a `b0 = 0x02` action word is the standard HID boot-protocol modifier
# bitmap, byte 0 of a keyboard report. Confirmed by OEM capture
# (data/oem_comapture_combo.log): Ctrl+V -> `02 01 19 00`, Shift+A ->
# `02 02 04 00`, Alt+X -> `02 04 1b 00`, Ctrl+Shift+Z -> `02 03 1d 00`.
# The fourth sample is the sum of the first two, which is what makes this a
# bitmask rather than an enum. Bits 0-3 are read straight off the capture; the
# right-hand bits are the standard HID assignments and are NOT yet confirmed
# on this device.
MOD_CTRL = 0x01
MOD_SHIFT = 0x02
MOD_ALT = 0x04
MOD_GUI = 0x08          # the Windows / GUI key
MOD_RCTRL = 0x10
MOD_RSHIFT = 0x20
MOD_RALT = 0x40
MOD_RGUI = 0x80

# (bit, label) for the modifiers offered in the GUI. Only the four left-hand
# ones: the right-hand bits are unverified on this device, so they are named in
# MOD_* for completeness but not offered.
MODIFIER_CHOICES = (
    (MOD_CTRL, "Ctrl"),
    (MOD_SHIFT, "Shift"),
    (MOD_ALT, "Alt"),
    (MOD_GUI, "Win"),
)


def combo_word(usage: int, mods: int = 0) -> bytes:
    """The 4-byte action word for "press `usage`, optionally with modifiers".

    Identical to the plain-remap word `02 00 <usage> 00` except for B1, which
    carries the modifier bitmap. So a combo is not a separate action type: the
    firmware reaches it through the same b0 = 0x02 case, and the OEM app writes
    both through the same field.
    """
    return bytes([0x02, mods & 0xFF, usage & 0xFF, 0x00])


def parse_payload(payload: bytes) -> dict:
    """Return {idx: action_word} for every non-zero entry."""
    out = {}
    for i in range(ENTRIES):
        w = payload[i * 4:i * 4 + 4]
        if w != b"\x00\x00\x00\x00":
            out[i] = w
    return out


def describe(idx) -> str:
    """Human description of one key's current mapping from a parsed dict."""
    return name(idx)


def split_combo(word: bytes):
    """(usage, mods) for a `b0 = 0x02` word, or None if it is not one.

    The inverse of combo_word(): B2 is the key to send and B1 is the modifier
    bitmap. A plain remap and a combo are the same word with mods == 0, which
    is why this app treats them as one action with an optional modifier rather
    than two actions.
    """
    if len(word) != 4 or word[0] != 0x02:
        return None
    return word[2], word[1]


def entry_word(buf, idx):
    """The 4-byte action word for `idx`, or b'\\0\\0\\0\\0' if unset.

    One place to ask "what does this key do", so the GUI does not grow a
    separate test per action type as more of them are decoded.
    """
    if idx is None or idx < 0 or idx >= SENTINEL_IDX:
        return b"\x00\x00\x00\x00"
    return bytes(buf[idx * 4:idx * 4 + 4])


def describe_word(word: bytes) -> str:
    """'Ctrl+Shift+Z' / 'Scroll up' / 'unchanged' for one action word.

    Covers the three decoded action types. Anything else returns '' so the
    caller can say it is not understood rather than guess.
    """
    if word == b"\x00\x00\x00\x00":
        return "unchanged"
    # Hot keys share b0 with combos, so they must be tried first: their words
    # are otherwise indistinguishable from a plain Ctrl+<key> remap.
    hot = split_hotkey(word)
    if hot is not None:
        return HOTKEYS[hot][0]
    mods_combo = split_combo(word)
    if mods_combo is not None:
        usage, mods = mods_combo
        return (modifier_label(mods) + "+" if mods else "") + usage_label(usage)
    mouse = split_mouse(word)
    if mouse is not None:
        return MOUSE_FUNCTIONS[mouse][0]
    media = split_media(word)
    if media is not None:
        return MEDIA_FUNCTIONS[media][0]
    macro = split_playmacro(word)
    if macro is not None:
        return f"plays macro {macro}"
    if split_disable(word):
        return "disabled"
    return ""


def modifier_label(mods: int) -> str:
    """'Ctrl+Shift' for a modifier bitmap, '' for none."""
    names = [lbl for bit, lbl in MODIFIER_CHOICES if mods & bit]
    if not names and mods:
        return f"mods 0x{mods:02x}"
    return "+".join(names)


# ---------------------------------------------------------------------------
# LAYERS
#
# The firmware reads the action word for a key from one of two tables, picked
# per keystroke by FUN_00002022 (settings/export.txt:5266):
#
#   word at 0x933C == 0x55AA0000  ->  Top layer, 0x90C0 + key_index*4
#   word at 0xA6BC == 0x55AA0000  ->  Fn  layer, 0xA440 + key_index*4
#
# Both banks are 640 bytes with the same 160 x 4-byte layout, and both are
# gated by their OWN magic word -- which is exactly the four-byte SENTINEL that
# sits at offset 636 of a 640-byte payload (0x90C0+636 = 0x933C,
# 0xA440+636 = 0xA6BC).  Writing the sentinel is therefore not cosmetic: it is
# what arms the layer.  Both words already read 0x55AA0000 in the factory
# image, and data/oem_defaults/keymap.bin and wheel_b.bin both end in it.
#
# Top layer is written by opcode 0x0411.  The Fn layer is the region behind
# 0x0426/0x0427, which docs/PAYLOAD_OPCODES.md mislabels "wheel ring B" --
# the firmware has FUN_00002022 and the 0x0427 dispatcher FUN_00003bec both
# reading 0xA440, and wheel_b.bin is 640 bytes of zeros, i.e. an empty layer.
# ---------------------------------------------------------------------------

LAYER_TOP = "top"
LAYER_FN = "fn"
LAYERS = (LAYER_TOP, LAYER_FN)
LAYER_LABEL = {LAYER_TOP: "Top layer", LAYER_FN: "Fn layer"}

# The Fn layer's FACTORY contents, captured from a real "reset current profile"
# upload on 2026-10-03 (data/oem_reset_capture.log, the 0x0427 session). This
# is the first time opcode 0x0427 has ever been seen on the wire from the OEM
# app; the values below were already known from the archived firmware image as
# data/oem_defaults/wheel_b.bin, and this capture confirms those bytes are what
# the app really uploads. tools/check_fn_layer.py asserts the two agree.
#
# It is the Top layer's factory keymap with two changes:
#
#   * 94 of the 160 keys carry DISABLE_WORD, so Fn+<key> sends nothing. Those
#     are the keys the Top layer passes through.
#   * F1-F5 carry media words and PrtSc/Pause are remapped to ScrollLock and
#     Insert.
#
# tools/check_fn_layer.py asserts this reproduces both the captured payload and
# the wheel_b.bin asset byte for byte.
FN_DISABLED = (
    7, 8, 9, 10, 11, 12, 13, 19, 20, 21, 22, 23, 24, 25, 26, 27,
    28, 29, 30, 31, 32, 33, 34, 37, 39, 40, 41, 42, 43, 44, 45, 46,
    47, 48, 49, 50, 51, 52, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64,
    65, 66, 67, 68, 69, 70, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82,
    83, 84, 85, 86, 87, 88, 91, 93, 94, 95, 97, 98, 99, 100, 101, 102,
    103, 104, 105, 106, 109, 113, 116, 117, 118, 119, 120, 121, 122, 123,
)

# F1 is `03 83 01 00` -- media page usage 0x83 but with B2 = 0x01, which is why
# split_media() refuses to claim it. This capture shows that word on real
# hardware in the factory Fn layer, so the refusal is right and is no longer
# resting on an archived image alone.
FN_REMAPS = {
    2: bytes([0x03, 0x83, 0x01, 0x00]),      # F1
    3: bytes([0x03, 0xCD, 0x00, 0x00]),      # F2  play/pause
    4: bytes([0x03, 0xB7, 0x00, 0x00]),      # F3  next track
    5: bytes([0x03, 0xB6, 0x00, 0x00]),      # F4  previous track
    6: bytes([0x03, 0xB5, 0x00, 0x00]),      # F5  stop
    112: bytes([0x02, 0x00, 0x47, 0x00]),    # PrtSc -> Scroll Lock
    115: bytes([0x02, 0x00, 0x49, 0x00]),    # Pause -> Insert
}


def fn_default_payload():
    """The 640-byte factory Fn layer.

    DEFAULT for this region, not the Top layer's keymap. Using the Top layer's
    factory bytes here looked harmless -- both are 640 B and share the sentinel
    -- but the Fn layer ships 94 keys disabled, so "clear the Fn layer" would
    have re-enabled every one of them and "apply" would have written that
    change to the keyboard.
    """
    buf = bytearray(default_payload())
    for idx in FN_DISABLED:
        buf[idx * 4:idx * 4 + 4] = DISABLE_WORD
    for idx, word in FN_REMAPS.items():
        buf[idx * 4:idx * 4 + 4] = word
    return bytes(buf)


def layer_default_payload(layer):
    """The factory payload for one layer, by layer id."""
    return fn_default_payload() if layer == LAYER_FN else default_payload()
LAYER_REGION = {LAYER_TOP: 0x90C0, LAYER_FN: 0xA440}
LAYER_OPCODE = {LAYER_TOP: 0x0411, LAYER_FN: 0x0427}
# The same mapping the other way, for recognising which layer an upload was for.
LAYER_KEYMAP_TO_LAYER = {op: lid for lid, op in LAYER_OPCODE.items()}
# Offset of each layer's gate word inside its own 640-byte payload.
LAYER_GATE_OFFSET = {LAYER_TOP: 636, LAYER_FN: 636}


def layer_payload(layer: str) -> bytes:
    """Empty 640-byte payload for `layer` (zeros + the arming sentinel)."""
    if layer not in LAYERS:
        raise ValueError(f"unknown layer {layer!r}")
    buf = bytearray(default_payload())
    return bytes(buf)


def layer_build_payload(layer: str, remaps: dict) -> bytes:
    """Build one layer's 640-byte payload. Same encoding for both layers."""
    if layer not in LAYERS:
        raise ValueError(f"unknown layer {layer!r}")
    return build_payload(remaps)


def layer_gate_ok(payload: bytes, layer: str) -> bool:
    """True if `payload` carries its layer's arming magic word."""
    off = LAYER_GATE_OFFSET[layer]
    return payload[off:off + 4] == SENTINEL


# ---------------------------------------------------------------------------
# OS LAYOUTS
#
# A layout is an ordinary keymap: {source key_index: target HID usage}. Nothing
# new is needed on the wire -- these are written with the same
# `02 00 <HID usage> 00` word the remap editor already uses, which is the
# best-evidenced encoding on this device (536 + 189 + ... occurrences across
# data/hidcap_round*.log).
#
# A layout is keyed by the US-QWERTY cap on a physical key, NOT by the 26
# letters: Dvorak and Colemak both move letters onto the punctuation keys
# (Colemak puts O on the physical ';', Dvorak puts letters on , . / [ ] '), and
# a letters-only model cannot express either. Everything a preset does not name
# -- digits, F-keys, the rest of the punctuation -- keeps its US-QWERTY meaning.
#
# LIMITATION -- accented characters. The Keyboard/Keypad page (0x07) has no
# usage for a, o, u, e, c, n or ss, and none for a shifted symbol. Qwertz and
# Azerty therefore ship what they can and leave the rest pass-through; the
# accented characters have to come from the operating system's own layout. The
# gap is reported by layout_unrepresentable() and stated in the GUI rather than
# silently omitted or approximated with a nearby key.
# ---------------------------------------------------------------------------

# The physical index of each cap is derived from the XML rather than hardcoded,
# so a board-variant change moves the tables with it.
LETTER_ORDER = "qwertyuiopasdfghjklzxcvbnm"

# HID usage -> the US-QWERTY character that key produces unshifted. The XML's
# `code` attribute is the real HID usage; its `name` is the printed legend, and
# on this board the legend shows the SHIFTED character (the comma key is named
# "<", the period key ">", the slash key "?", the backtick key "~"). Matching
# on the name therefore finds no comma and no period and every punctuation
# remap drops out, so the usage is the only reliable source.
USAGE_BY_CAP = {0x04: "a", 0x05: "b", 0x06: "c", 0x07: "d", 0x08: "e",
                0x09: "f", 0x0a: "g", 0x0b: "h", 0x0c: "i", 0x0d: "j",
                0x0e: "k", 0x0f: "l", 0x10: "m", 0x11: "n", 0x12: "o",
                0x13: "p", 0x14: "q", 0x15: "r", 0x16: "s", 0x17: "t",
                0x18: "u", 0x19: "v", 0x1a: "w", 0x1b: "x", 0x1c: "y",
                0x1d: "z",
                0x2d: "-", 0x2e: "=", 0x2f: "[", 0x30: "]", 0x31: "\\",
                0x32: "#", 0x33: ";", 0x34: "'", 0x35: "`", 0x36: ",",
                0x37: ".", 0x38: "/"}

# HID usages for the characters a layout may need, page 0x07. There is no usage
# for an accented character; a preset that wants one is skipped rather than
# approximated, and reported by layout_unrepresentable().
_PUNCT = {"'": 0x34, '"': 0x34, ",": 0x36, ".": 0x37, ";": 0x33, "/": 0x38,
          "-": 0x2d, "_": 0x2d, "[": 0x2f, "{": 0x2f, "]": 0x30, "}": 0x30,
          "\\": 0x31, "|": 0x31, "`": 0x35, "~": 0x35, "#": 0x32, "=": 0x2e,
          "+": 0x2e, " ": 0x2c}
USAGE_BY_LETTER = {chr(ord("a") + i): 0x04 + i for i in range(26)}

# cap char -> physical key_index, for letters AND punctuation, taken from the
# HID usage. The 26 letters are all present; the punctuation set is whatever
# the board actually has (it has , . / but no separate dead keys).
QCAP_INDEX = {}
for _i, (_u, _n) in sorted(KEYS.items()):
    _c = USAGE_BY_CAP.get(_u)
    if _c and _c not in QCAP_INDEX:
        QCAP_INDEX[_c] = _i

# The 26 letter positions, in US-QWERTY order: [(key_index, cap), ...]
_LETTER_POSITIONS = [(QCAP_INDEX[c], c) for c in LETTER_ORDER if c in QCAP_INDEX]


def _usage_for_char(ch):
    """HID usage for a layout character, or None if there isn't one."""
    if "a" <= ch <= "z":
        return USAGE_BY_LETTER[ch]
    if "A" <= ch <= "Z":
        return USAGE_BY_LETTER[ch.lower()]
    return _PUNCT.get(ch)


# Each preset is an explicit "US-QWERTY cap" -> "character it should output"
# map. Only the differences are listed: a cap absent from a preset keeps its
# US meaning. A value of None marks a character the keyboard cannot produce.
_LAYOUT_MAPS = {
    "QWERTY": {},
    # Colemak: the three letter rows reorder and ; moves up to the P key.
    # The physical ; and ' keys give up their old characters to O and /.
    "Colemak": {"e": "f", "r": "p", "t": "g", "y": "j", "u": "l", "i": "u",
                "o": "y", "p": ";", "s": "r", "d": "s", "f": "t", "g": "d",
                "j": "n", "k": "e", "l": "i", ";": "o", "n": "k", "'": "/"},
    # Dvorak (US). Punctuation moves into the letter rows and the old letter
    # keys hand their letters over to it.
    "Dvorak": {"q": "'", "w": ",", "e": ".", "r": "p", "t": "y", "y": "f",
               "u": "g", "i": "c", "o": "r", "p": "l", "s": "o", "d": "e",
               "f": "u", "g": "i", "h": "d", "j": "h", "k": "t", "l": "n",
               ";": "s", "z": ";", "x": "q", "c": "j", "v": "k", "b": "x",
               "n": "b", "m": "m", ",": "w", ".": "v", "/": "z", "'": "-"},
    # Qwertz (German). In ASCII the only difference from US-QWERTY is Z and Y
    # swapping; the umlauts and eszett are not HID usages, so they are listed
    # as None and the gap is deliberate rather than silent.
    "Qwertz": {"y": "z", "z": "y", ";": "ö", "'": "ä",
               "[": "ü", "]": None, "-": "ß"},
    # Azerty (French). The left-hand block transposes, M moves onto the
    # physical ;, and the accents are None for the same reason as Qwertz.
    "Azerty": {"q": "a", "w": "z", "a": "q", "z": "w", "m": ",",
               ";": "m", "'": "ù", "[": "°", "]": None,
               ",": "?", ".": "!", "/": "¨"},
    # A Mac keyboard has the same letters in the same places as US-QWERTY. The
    # OEM offers a Mac option only because it encodes targets in its own
    # mode-dependent byte2 table; ours is mode-independent, so Mac == QWERTY.
    "Mac": {},
}

LAYOUT_NAMES = tuple(_LAYOUT_MAPS)
LAYOUT_NOTES = {
    "QWERTY": "standard US letters",
    "Colemak": "letters reorder; , . / keep their US positions",
    "Dvorak": "letters and punctuation move together",
    "Qwertz": "Z and Y swap; umlauts and eszett need a German OS layout",
    "Azerty": "letters transpose; accents need a French OS layout",
    "Mac": "same letters as QWERTY - our encoding is mode-independent",
}


def layout_unrepresentable(layout: str) -> tuple:
    """Characters the preset wants that this keyboard cannot emit.

    These keys are left as pass-through when the payload is built. Returned so
    the GUI can say so instead of quietly delivering a half-layout.
    """
    out = []
    for src, want in _LAYOUT_MAPS.get(layout, {}).items():
        if want is not None and _usage_for_char(want) is None:
            out.append(want)
    return tuple(out)


def layout_remaps(layout: str) -> dict:
    """Return {source key_index: target HID usage} for a named layout.

    Only keys that differ from US-QWERTY appear in the result, so a layout is
    a sparse overlay and unchanged keys stay pass-through. Caps whose target
    character has no HID usage are omitted; see layout_unrepresentable().
    """
    if layout not in _LAYOUT_MAPS:
        raise ValueError(f"unknown layout {layout!r}")
    out = {}
    for src, want in _LAYOUT_MAPS[layout].items():
        if src not in QCAP_INDEX or want is None:
            continue
        if want == src:
            continue                      # a no-op belongs in neither map nor wire
        usage = _usage_for_char(want)
        if usage is not None:
            out[QCAP_INDEX[src]] = usage
    return out


def layout_payload(layout: str, base: dict | None = None) -> bytes:
    """640-byte payload for `layout`.

    `base` is an existing {idx: usage} remap dict to start from, so a layout
    can be applied without discarding mappings it does not touch.
    """
    merged = dict(base or {})
    merged.update(layout_remaps(layout))
    return build_payload(merged)


def layout_label(layout: str) -> str:
    return f"{layout} - {LAYOUT_NOTES.get(layout, '')}"


def usage_char(usage: int) -> str:
    """The character a layout remap to `usage` produces, or '' if unknown.

    Letters come back upper case to match the printed legends the canvas
    draws, punctuation unshifted so a layout that puts ',' on the W key says
    ','. USAGE_BY_CAP is the authority: it is derived from the XML usage
    codes, where the `name` attribute holds the SHIFTED legend, so reading the
    name instead would find no comma and no period.
    """
    ch = USAGE_BY_CAP.get(usage)
    if ch is None:
        return ""
    return ch.upper() if "a" <= ch <= "z" else ch


def layout_legends(layout: str) -> dict:
    """{key_index: character} -- where a layout moves each cap.

    The canvas counterpart of layout_remaps(): same keys, but the target
    usage resolved to the character it types, so a preview can redraw the
    board under the new layout. Sparse, like layout_remaps(), so caps the
    layout does not name are absent and keep their US legend.
    """
    out = {}
    for idx, usage in layout_remaps(layout).items():
        ch = usage_char(usage)
        if ch:
            out[idx] = ch
    return out


# ---------------------------------------------------------------------------
# PER-KEY ACTIONS
#
# The OEM app offers nine per-key actions (its own string table,
# software-exe-extract/.../language/1033.lan:290-301). The firmware models
# them as b0, an action type fed to an 8-case switch at 0x20e8
# (settings/export.txt:5376); b0 == 0 is the unmodified key.
#
# Only TWO of those encodings have ever been OBSERVED ON THE WIRE. Every
# archived capture (data/hidcap_round1/2/3.log, data/oem_capture_startup.log)
# was parsed and yields just:
#
#     02 00 <HID usage> 00   map this key to another key   (b0 = 0x02)
#     05 02 <byte2>      00   OEM mode-dependent byte2 table (b0 = 0x05)
#
# That is a statement about the captures, not about the firmware. The firmware
# dispatches on b0 through an 8-case switch at 0x20e8 whose bodies ARE readable
# -- Ghidra failed on the jumptable in all four builds and that failure was
# mistaken for missing data, but the switch is ARM's __ARM_common_switch8 and
# the image contains that routine's own implementation at 0x5ed8, so the table
# format and all eight case addresses follow. See ACTION_CASE below,
# tools/decode_action_switch.py, and docs/KEYMAP.md.
#
# What is still missing is not the code but the MEANING: the eight cases are
# decoded, yet attaching OEM action names to them needs (a) the mapping from
# ten labels onto eight cases and (b) a trace from each case's state[] writes to
# the bytes FUN_00002264 puts in the HID report. data/oem_defaults/macro.bin is
# still 512 zero bytes -- no recorded macro, text or delay sample either.
# Inventing those bytes would mean guessing at a device that has already been
# wedged once by unknown bytes (docs/PAYLOAD_OPCODES.md section 7). So the
# remaining seven stay listed and disabled, now for a narrower reason.
#
# Three modes are now capture-confirmed and implemented:
#   key    02 <mods=0> <hid usage> 00
#   combo  02 <mods>    <hid usage> 00     same word, B1 = HID modifier bitmap
#   mouse  01 <sub>     <function>  00     see MOUSE_FUNCTIONS
#
# docs/ACTION_MODES.md holds the per-mode working notes and the input design
# for each (button grids for the fixed lists, a field for text and paths, the
# board itself for key targets -- never a long dropdown).
# ---------------------------------------------------------------------------

# Mouse functions: the `b0 = 0x01` action, which the firmware reaches at 0x2216.
# B1 splits it in two -- 0x01 is a button press, 0x03 is the wheel -- and B2 is
# the function. Every row below is a word the OEM app was seen writing
# (data/oem_combo_capture.log); the pairing comes from the order the functions
# appear in the vendor's own list, 1033.lan strings 331-338.
#
# B2 is a bitmask, not a list index: "Right Click" (0x02) and "Mouse Button 4"
# (0x08) are separate bits, and "Scroll down" is 0xff, i.e. every bit of a byte
# at once, which is what a wheel-down step looks like when the driver is told
# "scroll" rather than "scroll by one". That is why these are named by function
# and not numbered 1..8.
MOUSE_LEFT = 0x01
MOUSE_RIGHT = 0x02
MOUSE_BOTH = 0x03          # the OEM's "Right-Left-Click"
MOUSE_SCROLL_UP = 0x01     # B2, in the wheel sub-mode
MOUSE_SCROLL_DOWN = 0xFF   # B2, in the wheel sub-mode
MOUSE_BUTTON4 = 0x08
MOUSE_BUTTON5 = 0x10

# B1 = 0x01 is a button, B1 = 0x03 is the wheel. Both confirmed.
MOUSE_SUB_BUTTON = 0x01
MOUSE_SUB_WHEEL = 0x03

# (label, B1, B2) in the vendor list's order (1033.lan 331-338).
MOUSE_FUNCTIONS = (
    ("Left click", MOUSE_SUB_BUTTON, MOUSE_LEFT),
    ("Middle click", MOUSE_SUB_BUTTON, 0x04),
    ("Right click", MOUSE_SUB_BUTTON, MOUSE_RIGHT),
    ("Right + left click", MOUSE_SUB_BUTTON, MOUSE_BOTH),
    ("Scroll up", MOUSE_SUB_WHEEL, MOUSE_SCROLL_UP),
    ("Scroll down", MOUSE_SUB_WHEEL, MOUSE_SCROLL_DOWN),
    ("Mouse button 4", MOUSE_SUB_BUTTON, MOUSE_BUTTON4),
    ("Mouse button 5", MOUSE_SUB_BUTTON, MOUSE_BUTTON5),
)


# Media keys: the `b0 = 0x03` action, and the only mode whose payload is a
# plain HID usage from a page other than the keyboard page. B1 is the
# Consumer Page (0x0C) usage and B2 is 0.
#
# Every value is a standard HID Consumer Page usage, and the mapping is
# confirmed twice over: by assigning all seven vendor media keys
# (1033.lan 366-372) and diffing the 0x0411 writes, and independently by the
# archived Fn-layer factory image, which carries Play/Pause, Next Track,
# Previous Track and Stop on F2-F5 (data/oem_capture_startup.log) -- data that
# was captured months before this encoding was understood.
#
# The firmware case at 0x2132 confirms the shape: press ORs B1 into state[0x6e],
# release ANDs it back out, which is exactly a consumer-usage field in a report.
MEDIA_PLAY_PAUSE = 0xCD
MEDIA_STOP = 0xB5
MEDIA_NEXT = 0xB7
MEDIA_PREV = 0xB6
MEDIA_VOL_UP = 0xE9
MEDIA_VOL_DOWN = 0xEA
MEDIA_MUTE = 0xE2

# (label, Consumer Page usage) in the vendor list's order (1033.lan 366-372).
MEDIA_FUNCTIONS = (
    ("Play / pause", MEDIA_PLAY_PAUSE),
    ("Stop", MEDIA_STOP),
    ("Next track", MEDIA_NEXT),
    ("Previous track", MEDIA_PREV),
    ("Volume +", MEDIA_VOL_UP),
    ("Volume -", MEDIA_VOL_DOWN),
    ("Mute", MEDIA_MUTE),
)


def media_word(n: int) -> bytes:
    """The action word for MEDIA_FUNCTIONS[n]: 03 <consumer usage> 00 00."""
    _label, usage = MEDIA_FUNCTIONS[n]
    return bytes([0x03, usage & 0xFF, 0x00, 0x00])


def split_media(word: bytes):
    """The MEDIA_FUNCTIONS index `word` names, or None if it is not a media word.

    B2 must be 0. The archived F1 entry is `03 83 01 00` -- b0 = 0x03 with a
    non-zero B2 and a B1 that is not a consumer usage -- so matching on B1 alone
    would risk calling it a media key. It is deliberately not recognised.
    """
    if len(word) != 4 or word[0] != 0x03 or word[2] or word[3]:
        return None
    for n, (_l, usage) in enumerate(MEDIA_FUNCTIONS):
        if usage == word[1]:
            return n
    return None


# Disable: a `b0 = 0x05` word with B1 = 0x03. The keyboard emits nothing for
# the key, and the write is confirmed on hardware: the Fn layer ships with 94 of
# its 160 keys carrying exactly this word, and pressing Fn+W sends nothing.
#
# That is a real distinction from clearing. Clearing writes 00 00 00 00, which
# is pass-through -- the key works normally again. Disabling writes this word,
# and the key stays dead. The two are not interchangeable, so they are separate
# actions here too.
#
# CONSEQUENCE worth knowing: 94 of the Fn layer's keys arrive from the factory
# already disabled. This app's default Fn payload is all zeros, i.e. everything
# enabled, so applying the Fn layer over the factory image turns those 94 keys
# back on. That is what the layer is for, but it is not the state it shipped in.
DISABLE_WORD = b"\x05\x03\x00\x00"


def split_disable(word: bytes):
    """True if `word` means "this key sends nothing"."""
    return bytes(word) == DISABLE_WORD


# Hot keys: named shortcuts for the OEM's seven commands. They are NOT a
# separate action type -- each is an ordinary `b0 = 0x02` word whose B1 is the
# modifier and whose B2 is the key, i.e. exactly what combo_word() builds. Every
# one is a standard Windows shortcut (Ctrl+A, Ctrl+C, Ctrl+V, Ctrl+X, Win+D,
# Win+L, Ctrl+W).
#
# This corrects an earlier reading of the same capture, which paired the seven
# captured words against the order of 1033.lan strings 350-356 and concluded
# they were NOT combos. That string order is not the OEM's list order: the
# capture assigns Select all, Copy, Paste, Cut, Show desktop, Lock computer,
# Close window, and under that pairing all seven are the obvious shortcuts.
# Nothing was wrong with the capture; only the pairing was.
#
# Earlier speculation here that the list aliases the four physical hotkey keys
# (Select-all 90, Copy 18, Paste 36, Cut 54) is likewise disproved: the
# assignments landed on 90/18/36/54/17/35/53, i.e. Copy on 36 and Cut on 53.
#
# Because these are plain combos they add nothing to what combo_word() can
# already write. They are kept as named presets so the user does not have to
# know that "Show desktop" means Win+D.
#
# (label, modifier bitmap, target HID usage)
HOTKEYS = (
    ("Select all", MOD_CTRL, 0x04),
    ("Copy", MOD_CTRL, 0x06),
    ("Paste", MOD_CTRL, 0x19),
    ("Cut", MOD_CTRL, 0x1B),
    ("Show desktop", MOD_GUI, 0x07),
    ("Lock computer", MOD_GUI, 0x0F),
    ("Close window", MOD_CTRL, 0x1A),
)


def hotkey_word(n: int) -> bytes:
    """The action word for HOTKEYS[n] -- identical to the equivalent combo."""
    _label, mods, usage = HOTKEYS[n]
    return combo_word(usage, mods)


def hotkey_combo(n: int):
    """(mods, usage) for HOTKEYS[n], so it can be shown as a shortcut."""
    _label, mods, usage = HOTKEYS[n]
    return mods, usage


def split_hotkey(word: bytes):
    """The HOTKEYS index `word` names, or None if it is not one of the seven."""
    if len(word) != 4 or word[0] != 0x02:
        return None
    for n, (_l, mods, usage) in enumerate(HOTKEYS):
        if mods == word[1] and usage == word[2]:
            return n
    return None


def mouse_word(func: int) -> bytes:
    """The 4-byte action word for a MOUSE_FUNCTIONS entry index.

    `func` is the index into MOUSE_FUNCTIONS, not the raw B2: the wheel
    sub-mode carries a different B2 vocabulary than the button one, so the
    (B1, B2) pair has to come from the table rather than from B2 alone.
    """
    _label, sub, b2 = MOUSE_FUNCTIONS[func]
    return bytes([0x01, sub, b2 & 0xFF, 0x00])


def split_mouse(word: bytes):
    """The MOUSE_FUNCTIONS index `word` names, or None if it is not a mouse word.

    The inverse of mouse_word(). B3 is ignored: every captured word ends 00.
    """
    if len(word) != 4 or word[0] != 0x01:
        return None
    for n, (_l, sub, b2) in enumerate(MOUSE_FUNCTIONS):
        if sub == word[1] and b2 == word[2]:
            return n
    return None

ACTION_KEY = "key"
ACTION_TEXT = "text"
ACTION_COMBO = "combo"
ACTION_LAUNCH = "launch"
ACTION_MOUSE = "mouse"
ACTION_HOTKEY = "hotkey"
ACTION_MEDIA = "media"
ACTION_PLAYMACRO = "playmacro"
ACTION_DISABLE = "disable"
ACTION_RECORD = "record"

# (label, action id, available). Only "key" is implemented.
ACTION_TYPES = [
    ("Map to another key or shortcut", ACTION_KEY, True),
    ("Text (type a string)", ACTION_TEXT, False),
    # Confirmed by OEM capture: Ctrl+V -> 02 01 19 00, Shift+A -> 02 02 04 00,
    # Alt+X -> 02 04 1b 00, Ctrl+Shift+Z -> 02 03 1d 00. Same b0 as a plain
    # remap; B1 is the HID modifier bitmap. See MOD_* and combo_word().
    ("Combo key (with Shift/Ctrl/Alt)", ACTION_COMBO, True),
    ("Launch application", ACTION_LAUNCH, False),
    # Confirmed by OEM capture: left/mid/right/right-left click -> 01 01 xx 00,
    # scroll up/down -> 01 03 01/ff 00, buttons 4/5 -> 01 01 08/10 00.
    ("Mouse function", ACTION_MOUSE, True),
    # Confirmed: each is a plain b0=0x02 word, i.e. an ordinary combo. See HOTKEYS.
    ("Hot key (Show desktop, Copy, ...)", ACTION_HOTKEY, True),
    # Confirmed by OEM capture: all seven vendor media keys (1033.lan 366-372)
    # written as `03 <consumer usage> 00 00`, and independently corroborated by
    # the archived Fn-layer factory image. See MEDIA_FUNCTIONS.
    ("Media (Play/Pause, Volume, ...)", ACTION_MEDIA, True),
    # The binding word is capture-confirmed and works on hardware (five keys,
    # each played from a key).
    #
    # No button of its own: it is reachable from "Record macro", whose panel
    # holds the recorder AND the slot box that writes this word. Two buttons
    # for one operation just made the choice look like a decision it is not.
    ("Play macro", ACTION_PLAYMACRO, True),
    # Recording is implemented (cloudnine_recorder); it shares the panel above.
    ("Record macro", ACTION_RECORD, True),
    # Confirmed on hardware: the Fn layer ships with this word on 94 of its 160
    # keys, and Fn+W sends nothing. Distinct from clearing, which is pass-through.
    ("Disable key", ACTION_DISABLE, True),
]

ACTION_AVAILABLE = {a: ok for _lbl, a, ok in ACTION_TYPES}
ACTIONS_PENDING = tuple(a for _lbl, a, ok in ACTION_TYPES if not ok)

# Implemented, and listed above, but deliberately without a button of its own.
# "Play macro" is not pending and not refused -- it is what the slot box in the
# Record macro panel writes -- it is simply not worth a second button for the
# same job. Kept apart from NOT_OFFERED, which means "we tried this and it
# cannot work", because that is not true here.
NO_BUTTON_ACTIONS = (ACTION_PLAYMACRO,)

# Modes the OEM app supports and this one investigates, but deliberately does
# not offer a button for. Both were captured; neither produced anything this app
# can act on, and leaving them in the list would only invite a user to build a
# mapping that cannot be made to work.
#
# text    assigning a string in the OEM app wrote NOTHING to the device: no
#         0x0411 change, no 0x0415 macro write, and the ASCII never appeared in
#         a single frame. Either the app needs a confirm step that was not found
#         or it keeps strings locally. The encoding is still unknown.
# launch  assigning applications wrote only slot indices -- a running sequence
#         `05 02 45 00`..`05 02 49 00`, with `03 92 01 00` for the calculator
#         preset -- and NO 0x0415 write in the whole session. The application's
#         name never reached the keyboard.
#
# On-device behaviour settles why launch is worse than merely unknown: with the
# OEM app running, its keys work; after closing it they stop, except the
# calculator preset. So the allocated slots depend on state the desktop app was
# maintaining and do not survive it, while the one fixed preset is resident in
# the keyboard. Offering a Launch button would mean offering something that
# silently dies when the app is closed.
#
# Kept as ids rather than deleted so the record of what was tried survives, and
# so restoring the buttons is a one-line change if an encoding turns up.
# Macros: a flat list of 8-byte key events in the macro region at 0x97C0,
# written with opcode 0x0415. Decoded from an OEM capture in which "a s d" was
# recorded twice -- once by hand, once with a fixed 10 ms delay
# (data/oem_macro_capture.log).
#
# Each 8-byte record is:
#
#   [0]     HID usage of the key
#   [1]     0xB0 = key down, 0x30 = key up
#   [2..3]  little-endian delay in MILLISECONDS
#   [4]     0x00
#   [5]     0x50 = another record follows, 0x00 = this is the last one
#   [6..7]  0x00
#
# The delay field is proved twice over: with a fixed 10 ms delay every record
# holds 0x000a, while a hand-typed recording holds the real inter-key gaps
# (160, 1368, 144, 1576, 128, 0 ms) -- which is only explicable as timing.
#
# The buffer is addressed by a little-endian 16-bit pointer in bytes 0..1, which
# locates a block whose first byte is 0x0b and whose events begin 10 bytes
# later. What 0x0b means is still unknown; both captured macros carry it.
#
# NOT YET USABLE as a feature, for two separate reasons. The binding word now
# exists and is confirmed (see playmacro_word), but:
#   * recording needs a global key-capture hook this app does not have, and
#   * only macro slot 0's layout is known. Later slots sit at an address with no
#     pointer to it -- bytes 0-1 hold one LE16 offset (0x0190 = 400) and there is
#     no second pointer anywhere in the 576-byte region -- so the stride between
#     macros is not yet derivable.
MACRO_DOWN = 0xB0
MACRO_UP = 0x30
MACRO_MORE = 0x50          # byte 5: another record follows
MACRO_LAST = 0x00          # byte 5: last record
MACRO_RECORD_SIZE = 8
# **The block header is a LENGTH FIELD, and this constant was the bug.**
#
# The first byte of a macro's block -- the byte at the offset the slot pointer
# names, ten bytes before the records -- is `2 * n - 1` for that block's own
# record count `n`. Verified against every block in every captured OEM region:
# 6 records -> 11, 10 -> 19, 12 -> 23, 13 -> 25, 24 -> 47. Every one fits.
#
# It was hardcoded to 0x0B (11), which is correct only for a six-record macro, so
# **every macro this app ever uploaded was told to play six records** however
# long it was. That is the whole of the "it always stops after three keys"
# behaviour: `asdzxc` gave `asd`, `asdASD`+Ctrl+A/C/V gave `asd`, and an
# 18-record `ASDASD` recorded at fixed 10 ms -- 898 ms, no timing involved --
# also stopped at six (2026-10-10).
#
# So the firmware does NOT derive a macro's length from the next slot's pointer.
# It reads the count out of the block header. Every theory built on the pointer
# table -- that a missing terminator clipped the last slot, that there was a
# per-slot capacity, that a playback time budget applied -- was chasing the wrong
# byte.
# The field is one byte wide, so `2n-1` runs out at n = 128 records. No capture
# has ever held a macro that long -- the longest is 24 -- and there is no second
# byte observed to carry the rest, so a macro that long cannot be described and
# is refused rather than written with a length the firmware would misread.
MACRO_MAX_RECORDS = 128


def macro_block_header(n: int) -> int:
    """The block's length field, `2n-1`, or a clear error past 128 records."""
    if n < 1:
        raise ValueError(f"a macro needs at least one record, got {n}")
    if n > MACRO_MAX_RECORDS:
        raise ValueError(
            f"a macro of {n} records does not fit the block's one-byte length "
            f"field (2n-1 overflows above {MACRO_MAX_RECORDS} records); the "
            f"firmware has no observed way to express a longer macro")
    return 2 * n - 1
MACRO_EVENTS_OFFSET = 10   # events start this far into the pointed-at block
MACRO_AREA_OFFSET = 400    # what bytes 0-1 point at (0x0190)
MACRO_FIRST_EVENT = MACRO_AREA_OFFSET + MACRO_EVENTS_OFFSET   # 410 = 0x19A
MACRO_BLOCK_PAD = 4        # step from one block's START to the next's
#
# Note what MACRO_BLOCK_PAD really means: it is the gap between block *starts*,
# not between one macro's records and the next. Block i+1's start therefore lands
# **inside** block i's records, six bytes before they end -- on the last record's
# delay_lo byte, which is where its length field goes.
#
# That is not a mistake in this file; it is what the OEM app writes. In
# data/oem_alt_capture.log every slot's length field overwrites the previous
# macro's last delay, and those macros play correctly: the firmware ignores that
# record's delay because byte 5 marks it MACRO_LAST.
#
# The visible consequence is on the way back in: `parse_macro_region()` reports
# the overwritten byte, so a parsed macro's final delay is the *next* block's
# length field, not a delay. Round-tripping OEM regions has always shown this.
MACRO_SENTINEL = b"\x00\x00\xaa\x55"     # tail of the whole region
# Every region is padded to at least this, whatever it actually needs.
#
# It must be the LARGEST size the OEM app has ever been seen to write, because a
# shorter write does not shrink the region: the bytes past the end of our
# payload are left exactly as the device already had them. The OEM app grows the
# region as macros are added -- 512 B empty, then 576, 704, 768, 832, 960, 1024
# and 1088 (data/oem_alt_capture.log) -- so a device that has held a six-macro
# library still has 1088 B of region, and a 1024 B write leaves its last 64
# bytes untouched.
#
# That matters because of the end marker. **Every captured OEM region contains
# exactly one `00 00 aa 55`, at its very end** (asserted for all eleven regions
# in every capture). A 1024 B write over a 1088 B device left the OEM's own
# marker still sitting at 0x43c while ours sat at 0x3fc: two end markers in one
# region, which no capture has ever shown and nothing here explains. Macro
# playback was never seen to complete, and it looped instead.
#
# So this must be 1088, the largest the OEM app has written. It was 1024 from a
# comment claiming 1024 was the largest, which was already wrong when written.
MACRO_REGION_MIN = 1088


MACRO_SLOT_STRIDE = 4      # bytes between consecutive slot pointers

# Where each macro lives. The region opens with a pointer TABLE: a little-endian
# 16-bit block offset every MACRO_SLOT_STRIDE bytes, one slot per macro. Each
# block's records begin MACRO_EVENTS_OFFSET bytes after its pointer, and the
# blocks are packed back to back, each padded with MACRO_BLOCK_PAD zero bytes.
#
#   slot 0  pointer 0x0190 (400)  -> records at 0x19A
#   slot 1  pointer 0x01C4 (452)  -> records at 0x1CE
#   slot 2  pointer 0x01F8 (504)  -> records at 0x202
#   slot 3  pointer 0x024C (588)  -> records at 0x256
#
# so the step between blocks is VARIABLE. With macros of 6, 6 and 10 records the
# gaps were 52 and 52, each exactly records*8 + 4; the fourth sat at 0x256 =
# 0x202 + 80 + 4 and parsed as a clean 12-record block, where a fixed 52-byte
# stride would have landed mid-record in garbage.
#
# This corrects an earlier note here that said only one pointer existed and that
# later macros had none. They do: the search was for the records offset rather
# than the block offset the pointer actually holds.
MACRO_SLOT_EMPTY = 0xFFFF   # what an unused slot's pointer reads as


def macro_block_offset(index: int, macros) -> int:
    """Byte offset of macro `index`'s records within the region."""
    off = MACRO_FIRST_EVENT
    for events in macros[:index]:
        off += len(events) * MACRO_RECORD_SIZE + MACRO_BLOCK_PAD
    return off


def macro_slot_pointer(offset: int) -> bytes:
    """The 4-byte slot entry: a 16-bit block offset then two zero bytes."""
    return offset.to_bytes(2, "little") + b"\x00\x00"


def macro_slot_count(payload: bytes) -> int:
    """How many slots the pointer table at the front of `payload` uses."""
    n = 0
    while (n * MACRO_SLOT_STRIDE + 2) <= len(payload):
        if int.from_bytes(payload[n * MACRO_SLOT_STRIDE:
                                 n * MACRO_SLOT_STRIDE + 2],
                           "little") in (0x0000, MACRO_SLOT_EMPTY):
            break
        n += 1
    return n


def macro_record(usage: int, down: bool, delay_ms: int = 0):
    """One 8-byte macro event record."""
    return bytes([usage & 0xFF, MACRO_DOWN if down else MACRO_UP,
                  delay_ms & 0xFF, (delay_ms >> 8) & 0xFF,
                  0x00, 0x00, 0x00, 0x00])


def macro_events(events):
    """Pack [(usage, is_down, delay_ms), ...] into the macro block records.

    The last record gets MACRO_LAST in byte 5 and every other one MACRO_MORE,
    which is how the firmware knows where the macro ends. Returns just the
    records; the caller places them in the region.
    """
    out = bytearray()
    n = len(events)
    for i, (usage, down, delay) in enumerate(events):
        rec = bytearray(macro_record(usage, down, delay))
        rec[5] = MACRO_LAST if i == n - 1 else MACRO_MORE
        out += rec
    return bytes(out)


def parse_macro_events(blob: bytes):
    """[(usage, is_down, delay_ms), ...] from a block of macro records.

    Stops at the first all-zero record or at MACRO_LAST in byte 5.
    """
    out = []
    for i in range(0, len(blob) - MACRO_RECORD_SIZE + 1,
                   MACRO_RECORD_SIZE):
        rec = blob[i:i + MACRO_RECORD_SIZE]
        if not any(rec):
            break
        out.append((rec[0], rec[1] == MACRO_DOWN,
                    int.from_bytes(rec[2:4], "little")))
        if rec[5] != MACRO_MORE:
            break
    return out


def macro_pointer(block_offset: int) -> bytes:
    """The two header bytes that address a macro block at `block_offset`."""
    return (block_offset & 0xFFFF).to_bytes(2, "little")


# Play macro: `b0 = 0x06`. Confirmed on hardware -- "asd" and a second macro
# "test2" were each recorded, assigned to keys, and played.
#
#     06 00 00 01   key plays macro 0
#     06 01 01 01   key plays macro 1
#
# B2 is the macro index: 0x00 held "asd" on two keys and 0x01 held "test2" on two
# others, and pressing them played the matching macro. B3 was 0x01 on all four,
# which fits the vendor's "Select run time" list (1033.lan 381-396: Play One
# Time / Play Multiple / Loop count / Disable / Stop when key again) with 1 =
# Play One Time.
#
# B1 is NOT understood: it was 0x00 on three of the four keys and 0x01 on the
# fourth, and all four played, so it does not select the macro. The captured
# words all work with B1 = 0, which is what playmacro_word() writes.
#
# This corrects an earlier reading of the firmware: the case at 0x217e appears to
# require B1 == 2 and so should ignore these words, yet the keyboard plays them.
# The hardware wins over the disassembly here.
MACRO_PLAY_ONCE = 0x01


def parse_macro_region(payload: bytes):
    """The macros in a macro region, in slot order.

    Each entry is that macro's event list. Slots are packed back to back from
    MACRO_FIRST_EVENT, each followed by MACRO_BLOCK_PAD zero bytes; the walk
    stops at the first empty record.
    """
    macros = []
    for slot in range(macro_slot_count(payload)):
        blk = int.from_bytes(payload[slot * MACRO_SLOT_STRIDE:
                                     slot * MACRO_SLOT_STRIDE + 2],
                             "little")
        events = parse_macro_events(payload[blk + MACRO_EVENTS_OFFSET:])
        if not events:
            break
        macros.append(events)
    return macros


def build_empty_macro_region() -> bytes:
    """A macro region with no macros in it, byte for byte as the OEM app writes it.

    Reproduces the 512-byte empty region captured in
    data/oem_playmacro_capture.log: a single slot pointer of 0xFFFFFFFF, then
    zeros to the `00 00 aa 55` tail. tools/check_macro_upload.py asserts this
    matches the capture exactly.

    **This is the way to clear macros off a keyboard whose macro will not stop
    replaying.** An empty region has no block for any key to point into, so
    pressing a key bound to a macro slot has nothing to play. It is also the only
    macro payload in this file that is known to be byte-correct on hardware
    without having been watched mid-playback.
    """
    return (b"\xff\xff\xff\xff".ljust(512 - len(MACRO_SENTINEL), b"\x00")
            + MACRO_SENTINEL)


def build_macro_region(macros) -> bytes:
    """A macro region holding `macros`, in the layout the OEM app writes.

    **A terminator pointer is written one slot past the last macro**, marking
    the end of the final block. This is the whole ballgame, and it was wrong
    twice before being right.

    The firmware gives a slot exactly as much room as the gap to the *next*
    slot's block allows. Every slot in every captured OEM region is **exactly
    full** -- 6 records in 52 bytes, 10 in 84, 12 in 100, 24 in 196, each the
    macro's records plus the 4-byte pad -- with the single exception of the last
    slot, which has no next pointer. The OEM app never notices, because the
    newest macro is always last and has never been watched playing.

    This app has one macro, always in slot 0, which *is* the last slot. With
    nothing after it, the firmware fell back to a 52-byte stride and every
    macro played exactly **6 records** -- `asdzxc` gave `asd`, `asdASD`+Ctrl gave
    `asd`, and an 18-record `ASDASD` recorded at fixed 10 ms -- 898 ms, well
    inside any time budget -- still stopped at 6 (2026-10-10). Six records was
    never a time budget and never a per-slot capacity; it was the default stride
    with no next slot to measure against.

    The terminator restores the measurement. For a six-record macro it produces
    slot 1 = `0x1c4`, which is **byte for byte the pointer the OEM app itself
    wrote there** when a second macro followed -- so it is not an invention,
    it is the pointer the OEM would have written had the macro not been last.

    **It was withdrawn once and wrongly.** It was first added to stop playback
    looping, which turned out to be the region being 1024 B over a 1088 B device
    (two end markers -- see MACRO_REGION_MIN). Tested against that broken
    region it appeared to do nothing, so it was pulled. It was never tested
    against a correct region until now.
    """
    used = MACRO_FIRST_EVENT + sum(
        len(m) * MACRO_RECORD_SIZE + MACRO_BLOCK_PAD for m in macros)
    size = max(MACRO_REGION_MIN, (used + 8 + 63) // 64 * 64)
    buf = bytearray(size)
    off = MACRO_FIRST_EVENT
    for i, events in enumerate(macros):
        buf[i * MACRO_SLOT_STRIDE:i * MACRO_SLOT_STRIDE + 4] = \
            macro_slot_pointer(off - MACRO_EVENTS_OFFSET)
        # The length field: 2n-1 for this block's own record count.
        buf[off - MACRO_EVENTS_OFFSET] = macro_block_header(len(events))
        n = len(events) * MACRO_RECORD_SIZE
        buf[off:off + n] = macro_events(events)
        off += n + MACRO_BLOCK_PAD
    # The end of the last block, so the firmware can measure the last slot the
    # same way it measures every other one.
    end = len(macros) * MACRO_SLOT_STRIDE
    buf[end:end + 4] = macro_slot_pointer(off - MACRO_EVENTS_OFFSET)
    buf[size - len(MACRO_SENTINEL):] = MACRO_SENTINEL
    return bytes(buf)


def describe_macro(events, limit: int = 8) -> str:
    """A short human summary of a macro, for the picker."""
    if not events:
        return "(empty)"
    keys = [usage_label(u) for u, down, _d in events if down]
    head = " ".join(keys[:limit])
    return head + (f" +{len(keys) - limit}" if len(keys) > limit else "")


def playmacro_word(index: int) -> bytes:
    """The action word for "press this key plays macro `index`, once`."""
    return bytes([0x06, 0x00, index & 0xFF, MACRO_PLAY_ONCE])


def split_playmacro(word: bytes):
    """The macro index `word` plays, or None if it is not a play-macro word.

    Only recognises the shape actually observed: B0 = 0x06 with B1 = 0. A word
    with some other B1 is reported as unknown rather than guessed at, because
    what B1 means is not known.
    """
    if len(word) != 4 or word[0] != 0x06 or word[1] != 0x00:
        return None
    return word[2]


NOT_OFFERED = (ACTION_TEXT, ACTION_LAUNCH)

# What the firmware actually branches on: B0, the first byte of the 4-byte
# action word. The switch at 0x20e8 is ARM's __ARM_common_switch8 and its
# implementation is present in the image at 0x5ed8, so the case addresses are
# computable rather than lost -- see tools/decode_action_switch.py and
# docs/KEYMAP.md "The action switch". B0 == 0 is pre-checked before the switch,
# so the eight live cases are B0 = 1..8.
#
# NOTE this is a case-address map, not an action map. Attaching a GUI action id
# to a B0 needs two more things that do not exist yet: the mapping from the ten
# OEM labels onto eight cases (so at least two labels must share one), and the
# trace from each case's state[] writes to the bytes that reach the HID report
# in FUN_00002264. Until both are done, attaching a name here would be a guess,
# and a wrong guess is what wedged the MCU once (docs/PAYLOAD_OPCODES.md §7).
ACTION_CASE = {
    1: "swallow the key: movs r0,#0; pop. Emits nothing",
    2: "state[0x16]; B1 sub-mode: 1 = OR/AND B2 into state[0x59] on "
       "press/release, 3 = store B2 to state[0x5c]",
    3: "press: state[0x6e] |= B1, release: &= ~B1; state[0x15]=1, state[8]=B2",
    4: "state[0x17]; on press B1 -> state[0x37], B2 -> state[0x38]",
    5: "state[0x18]; on press B1 -> state[0x2e]",
    6: "B1==2 and press: state[0x48] = {5, 0, B2, 0}",
    7: "press: walks {0x97C0 + TABLE[B1], +8} into state[0x50..0x5b]. "
       "0x97C0 is the macro region, so this is a macro player",
    8: "press: state[0xd]=1, state[0x54] = 0x90C0 + r1 (the Top keymap bank) "
       "-- a layer select",
}


# ---------------------------------------------------------------------------
# NAMED LAYOUTS ON DISK
#
# A saved layout bundles both layers so switching layers back and forth does
# not lose the other one. Fixed-size container, so a truncated or wrong file is
# rejected outright rather than half-applied -- the same discipline the GUI
# already uses for keymap.bin (640) and config.bin (1152).
#
#   0  ..  7   b"CNLAYOUT"
#   8  .. 11   version, u32 LE (1)
#   12 .. 651  Top layer, 640 bytes
#   652 ..1291 Fn layer, 640 bytes
# ---------------------------------------------------------------------------

LAYOUT_MAGIC = b"CNLAYOUT"
LAYOUT_VERSION = 1
LAYOUT_HEADER = 12
LAYOUT_FILE_SIZE = LAYOUT_HEADER + 2 * KEYMAP_SIZE


def pack_named_layout(top: bytes, fn: bytes) -> bytes:
    """Bundle two layer payloads into a saved-layout file."""
    if len(top) != KEYMAP_SIZE or len(fn) != KEYMAP_SIZE:
        raise ValueError("each layer payload must be 640 bytes")
    return (LAYOUT_MAGIC + LAYOUT_VERSION.to_bytes(4, "little")
            + bytes(top) + bytes(fn))


def unpack_named_layout(blob: bytes):
    """Return (top_payload, fn_payload) or None if `blob` is not one of ours."""
    if len(blob) != LAYOUT_FILE_SIZE:
        return None
    if blob[:8] != LAYOUT_MAGIC:
        return None
    if int.from_bytes(blob[8:12], "little") != LAYOUT_VERSION:
        return None
    top = blob[LAYOUT_HEADER:LAYOUT_HEADER + KEYMAP_SIZE]
    fn = blob[LAYOUT_HEADER + KEYMAP_SIZE:LAYOUT_HEADER + 2 * KEYMAP_SIZE]
    return top, fn


# ---------------------------------------------------------------------------
# Standard HID keyboard usage catalog for the target picker.
# (usage, label) — labels copied from the USB HID Usage Tables 1.12, page 0x07.
# ---------------------------------------------------------------------------
HID_TARGETS = [
    (0x04, "A"), (0x05, "B"), (0x06, "C"), (0x07, "D"), (0x08, "E"),
    (0x09, "F"), (0x0a, "G"), (0x0b, "H"), (0x0c, "I"), (0x0d, "J"),
    (0x0e, "K"), (0x0f, "L"), (0x10, "M"), (0x11, "N"), (0x12, "O"),
    (0x13, "P"), (0x14, "Q"), (0x15, "R"), (0x16, "S"), (0x17, "T"),
    (0x18, "U"), (0x19, "V"), (0x1a, "W"), (0x1b, "X"), (0x1c, "Y"),
    (0x1d, "Z"), (0x1e, "1"), (0x1f, "2"), (0x20, "3"), (0x21, "4"),
    (0x22, "5"), (0x23, "6"), (0x24, "7"), (0x25, "8"), (0x26, "9"),
    (0x27, "0"), (0x28, "Enter"), (0x29, "Esc"), (0x2a, "Backspace"),
    (0x2b, "Tab"), (0x2c, "Space"), (0x2d, "- _"), (0x2e, "= +"),
    (0x2f, "[ {"), (0x30, "] }"), (0x31, "\\ |"), (0x32, "# ~"),
    (0x33, "; :"), (0x34, "' \""), (0x35, "` ~"), (0x36, ", <"),
    (0x37, ". >"), (0x38, "/ ?"), (0x39, "CapsLock"), (0x3a, "F1"),
    (0x3b, "F2"), (0x3c, "F3"), (0x3d, "F4"), (0x3e, "F5"), (0x3f, "F6"),
    (0x40, "F7"), (0x41, "F8"), (0x42, "F9"), (0x43, "F10"),
    (0x44, "F11"), (0x45, "F12"), (0x46, "PrintScreen"),
    (0x47, "ScrollLock"), (0x48, "Pause/Break"), (0x49, "Insert"),
    (0x4a, "Home"), (0x4b, "PageUp"), (0x4c, "Delete"), (0x4d, "End"),
    (0x4e, "PageDown"), (0x4f, "Right"), (0x50, "Left"), (0x51, "Down"),
    (0x52, "Up"), (0x53, "NumLock"), (0x54, "Num /"), (0x55, "Num *"),
    (0x56, "Num -"), (0x57, "Num +"), (0x58, "Num Enter"),
    (0x59, "Num 1"), (0x5a, "Num 2"), (0x5b, "Num 3"), (0x5c, "Num 4"),
    (0x5d, "Num 5"), (0x5e, "Num 6"), (0x5f, "Num 7"), (0x60, "Num 8"),
    (0x61, "Num 9"), (0x62, "Num 0"), (0x63, "Num . Del"), (0x64, "Num ="),
    (0x65, "App/Menu"), (0x85, "Macro 1"), (0x86, "Macro 2"),
    (0x88, "Macro 3"), (0x89, "Macro 4"), (0xe0, "LCtrl"), (0xe1, "LShift"),
    (0xe2, "LAlt"), (0xe3, "LWin"), (0xe4, "RCtrl"), (0xe5, "RShift"),
    (0xe6, "RAlt"), (0xe7, "RWin"),
]

HID_BY_USAGE = {u: lbl for u, lbl in HID_TARGETS}


def usage_label(usage: int) -> str:
    return HID_BY_USAGE.get(usage, f"usage 0x{usage:02x}")


# usage -> the character it types, so "o" can find the O key. Built from
# USAGE_BY_CAP (letters and the punctuation the board has) rather than from the
# HID_TARGETS labels, because those label a shifted symbol "# ~" where the
# character the key types is "#".
_CHAR_TO_USAGE = {}
for _u in sorted(USAGE_BY_CAP):
    _CHAR_TO_USAGE.setdefault(USAGE_BY_CAP[_u], _u)
del _u


def usage_matches(text: str) -> list:
    """[(usage, label)] for every catalog key a typed name could mean.

    Matching is deliberately two-tier so a name that is exact is never
    shadowed by a looser match: first the catalog label, then the character
    the key types, then a substring pass. The substring pass is what lets
    "ctrl" find LCtrl; it is only trusted when it leaves exactly one
    candidate, which is why "win" correctly stays ambiguous between LWin and
    RWin instead of silently picking one.
    """
    t = (text or "").strip()
    if not t:
        return []
    tl = t.lower()
    exact = [(u, l) for u, l in HID_TARGETS if l.lower() == tl]
    if exact:
        return exact
    if len(t) == 1:
        hit = _CHAR_TO_USAGE.get(t) or _CHAR_TO_USAGE.get(t.lower())
        if hit is not None:
            return [(hit, usage_label(hit))]
    return [(u, l) for u, l in HID_TARGETS if tl in l.lower()]


def find_usage(text: str):
    """The one HID usage `text` names, or None if it does not name exactly one."""
    hits = usage_matches(text)
    return hits[0][0] if len(hits) == 1 else None


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "dump":
        p = sys.argv[2]
        data = open(p, "rb").read()
        for i, w in parse_payload(data).items():
            if i == SENTINEL_IDX:
                continue
            src = name(i) or i
            if w[:2] == REMAP:
                lbl = usage_label(w[2])
            else:
                lbl = f"raw {w.hex(' ')}"
            print(f"  {i:3d} {src:<12} -> {lbl}")