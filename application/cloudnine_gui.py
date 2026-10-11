#!/usr/bin/env python3
"""Cloudnine ErgoFSK C989 — M1 lighting GUI.

PyQt6 front-end for the decoded `0c45:8006` lighting protocol. Implements the
M1 milestone of docs/GUI_MAPPING_PLAN.md:

  * 1:1 on-screen keyboard clone (LayoutK89M rects from docs/KEYMAP.md)
    at the top; Zone, Color, Effects, Parameters and Actions are columns
    below the keyboard
  * effect list (19 builtin modes + User-defined per-key)
  * palette picker + effect color / full-spectrum / brightness / speed / direction
  * Color column: fixed palette + 8 persisted custom swatches (left-click to
    use, right-click to save the current colour) + hex editor + CMY inputs
    (the pick dialog itself is the plain non-native picker, no custom colours)
  * per-key painter for the User-defined (0x80) region
  * zones listed in the Zone column (wasd, qwerdf, 0-9, numpad, macro, arrows,
    action/Ins group, F keys, left/right board, All) — click a zone to paint it
    with the current color, double-click (or Clear zone) to empty it; the
    per-key paint toggle lives here too
  * Apply (upload + 0x04F0 commit) via cloudnine_app.apply_config
  * load/save local config, restore OEM factory default
  * Firmware tab: flash the M10 / wheel-ring keyboard-firmware images (see
    docs/M10_FIX_FLASH.md); a release ships only the recommended image
  * last applied/saved state persists to ~/.config/cloudnine-c989/ and is
    restored on startup ("lighting appears correct"), because this device's
    read-back opcodes reply with zero-filled pages (no live state to fetch)

All writes use the documented OEM upload sequence (apply_region): each region
in its own session (lead -> header -> pages -> checksum -> commit). The 0x0402
reply is the acceptance signal on this board — only `04 02 00 01 <sum>` MATCH
sessions persist across a power cycle. The 0x04F0 commit is still sent in
commit mode regardless (matching the OEM wire capture), but on a refused
session (`04 02 00 ff`/`00`) it echoes `04 f0 00 00` and persists nothing, so
a non-reported checksum is reported as FAILED (see checksum_summary).
"""
import json
import math
import os
import subprocess
import sys
import threading

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QSize, QSignalBlocker
from PyQt6.QtGui import QColor, QPainter, QPen, QBrush, QFont, QFontMetrics
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QGridLayout,
    QListWidget, QListWidgetItem, QPushButton, QLabel, QSlider, QCheckBox,
    QComboBox, QColorDialog, QFileDialog, QMessageBox, QGroupBox, QFrame,
    QSizePolicy, QTabWidget, QSpinBox, QListView, QAbstractItemView, QLineEdit,
    QInputDialog, QButtonGroup, QTableWidget, QTableWidgetItem,
    QScrollArea,
)

import cloudnine_app
from cloudnine_app import (USER_ID, apply_config, apply_macro, apply_region,
                            asset_path,
                            checksum, checksum_summary, default_template,
                            firmware_path)
from cloudnine_keymap import (
    KEYS as KM_KEYS, HID_TARGETS, usage_label, usage_matches,
    combo_word, modifier_label, MODIFIER_CHOICES, ACTION_COMBO,
    mouse_word, split_mouse, MOUSE_FUNCTIONS, ACTION_MOUSE,
    hotkey_word, split_hotkey, HOTKEYS, ACTION_HOTKEY,
    playmacro_word, split_playmacro, ACTION_PLAYMACRO, ACTION_RECORD,
    media_word, split_media, MEDIA_FUNCTIONS, ACTION_MEDIA,
    DISABLE_WORD, split_disable, ACTION_DISABLE,
    entry_word, describe_word,
    default_payload as km_default,
    layer_default_payload as km_layer_default,
    UNMAPPABLE as KM_UNMAPPABLE, LIGHT_IDX, OEM_FW, V1_FW,
    LIGHT_CODE_INERT, firmware_light_code, firmware_light_patch_ok,
    LAYER_TOP, LAYER_FN, LAYER_LABEL, LAYERS, LAYER_OPCODE,
    LAYER_KEYMAP_TO_LAYER,
    layout_remaps, layout_payload, LAYOUT_NAMES, layout_label,
    layout_unrepresentable, layout_legends,
    ACTION_TYPES, ACTION_AVAILABLE, ACTION_KEY, ACTION_TEXT, NOT_OFFERED,
    NO_BUTTON_ACTIONS,
    pack_named_layout, unpack_named_layout, LAYOUT_FILE_SIZE,
)
from sonix_kbd import (
    Keyboard, OP_WRITE_COLORS, OP_WRITE_KEYMAP, OP_WRITE_MACRO,
    OP_WRITE_GAMING, OP_WRITE_WHEEL,
)
from cloudnine_recorder import (
    MacroRecorder, available as recorder_available,
    strip_stamps as rec_strip_stamps, recording_warnings,
)
from cloudnine_keymap import describe_macro as km_describe_macro
from cloudnine_keymap import (
    build_macro_region, build_empty_macro_region, parse_macro_region,
    macro_events as km_macro_events, usage_label as km_usage_label,
    MACRO_SENTINEL, MACRO_REGION_MIN, macro_block_header,
)

OP_COLORS = OP_WRITE_COLORS
OP_WHEEL = OP_WRITE_WHEEL

# The wheel-region write's `attempts` budget covers transport/session
# breakage (a 0x04F0 round-trip that fails to echo). A 0x0425 attempt is a
# single 64 B page, so 60 attempts at the 0.4 s retry delay is only ~25-30 s
# of wall time while still covering the worst documented transport failure with
# headroom. A *refusal* (`04 02 00 ff`/`00` at the 0x0402 gate) is the board's
# intermittent reject state, not a transport fault, and is retried on
# apply_region's own short budget (REFUSAL_RETRIES), not on this one.
WHEEL_ATTEMPTS = 60

# The C989's sessions are stateful and device-wide: a read session left running
# (unconsumed page fetches) corrupts the checksum ACK of a concurrent write
# session (WRITE answers `04 02 00 ff`, which is indistinguishable from a real
# refusal). Serialize every Keyboard session so Apply and the startup read
# can never collide.
_dev_lock = threading.Lock()

# The catalog labels spell out the detail in brackets -- "Combo key (with
# Shift/Ctrl/Alt)". A button has no room for that, so the buttons take the
# head and the whole line stays on the tooltip. Kept here rather than
# truncating in the loop so one place owns the rule.
_ACTION_BUTTON_OVERRIDES = {"Map to another key or shortcut": "Other key",
                            "Combo key (with Shift/Ctrl/Alt)": "Combo key"}


def action_button_label(label):
    """The short form of an action label, for a button face."""
    return _ACTION_BUTTON_OVERRIDES.get(label, label.split(" (")[0])

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CANDIDATES = [
    "/tmp/default_config.bin",
    asset_path("data", "oem_defaults", "colors_default.bin", required=False),
]
# The repo keeps flash.sh and the images in settings/FWFiles/; the packaged
# release puts them in Upload/firmware/. firmware_path() finds both.
FLASH_SH = firmware_path("flash.sh", required=False)

# ── LayoutK89M geometry (docs/KEYMAP.md): (light_index, name, x, y, w, h) ──────────
LAYOUT = [
    (1, "Esc", 122, 1, 50, 50), (2, "F1", 186, 1, 50, 50), (3, "F2", 238, 1, 50, 50),
    (4, "F3", 290, 1, 50, 50), (5, "F4", 342, 1, 50, 50), (6, "F5", 405, 1, 50, 50),
    (7, "F6", 570, 1, 50, 50), (8, "F7", 622, 1, 50, 50), (9, "F8", 674, 1, 50, 50),
    (10, "F9", 750, 1, 50, 50), (11, "F10", 802, 1, 50, 50), (12, "F11", 854, 1, 50, 50),
    (13, "F12", 906, 1, 50, 50),
    (17, "M6", 53, 78, 50, 50), (18, "M2", 1, 130, 50, 50), (19, "~", 122, 78, 50, 50),
    (20, "1", 174, 78, 50, 50), (21, "2", 226, 78, 50, 50), (22, "3", 278, 78, 50, 50),
    (23, "4", 330, 78, 50, 50), (24, "5", 382, 78, 50, 50), (25, "6", 434, 78, 50, 50),
    (26, "7", 543, 78, 50, 50), (27, "8", 595, 78, 50, 50), (28, "9", 647, 78, 50, 50),
    (29, "0", 699, 78, 50, 50), (30, "-", 751, 78, 50, 50), (31, "=", 803, 78, 50, 50),
    (32, "Num", 1150, 78, 50, 50), (33, "/", 1202, 78, 50, 50), (34, "*", 1254, 78, 50, 50),
    (35, "M7", 53, 130, 50, 50), (36, "M3", 1, 182, 50, 50), (37, "Tab", 122, 130, 75, 50),
    (38, "Q", 199, 130, 50, 50), (39, "W", 251, 130, 50, 50), (40, "E", 303, 130, 50, 50),
    (41, "R", 355, 130, 50, 50), (42, "T", 407, 130, 50, 50), (43, "Y", 519, 130, 50, 50),
    (44, "U", 571, 130, 50, 50), (45, "I", 623, 130, 50, 50), (46, "O", 675, 130, 50, 50),
    (47, "P", 727, 130, 50, 50), (48, "[", 779, 130, 50, 50), (49, "]", 831, 130, 50, 50),
    (50, "7", 1150, 130, 50, 50), (51, "8", 1202, 130, 50, 50), (52, "9", 1254, 130, 50, 50),
    (53, "M8", 53, 182, 50, 50), (54, "M4", 1, 234, 50, 50), (55, "Caps", 122, 182, 87, 50),
    (56, "A", 211, 182, 50, 50), (57, "S", 263, 182, 50, 50), (58, "D", 315, 182, 50, 50),
    (59, "F", 367, 182, 50, 50), (60, "G", 419, 182, 50, 50), (61, "H", 532, 182, 50, 50),
    (62, "J", 584, 182, 50, 50), (63, "K", 636, 182, 50, 50), (64, "L", 688, 182, 50, 50),
    (65, ";", 740, 182, 50, 50), (66, "'", 792, 182, 50, 50), (67, "|", 883, 130, 73, 50),
    (68, "4", 1150, 182, 50, 50), (69, "5", 1202, 182, 50, 50), (70, "6", 1254, 182, 50, 50),
    (71, "M9", 53, 234, 50, 50), (72, "M5", 1, 286, 50, 50), (73, "Shift", 122, 234, 113, 50),
    (74, "Z", 237, 234, 50, 50), (75, "X", 289, 234, 50, 50), (76, "C", 341, 234, 50, 50),
    (77, "V", 393, 234, 50, 50), (78, "B", 445, 234, 50, 50), (79, "N", 557, 234, 50, 50),
    (80, "M", 609, 234, 50, 50), (81, "<", 661, 234, 50, 50), (82, ">", 713, 234, 50, 50),
    (83, "?", 765, 234, 50, 50), (84, "Shift", 817, 234, 139, 50), (85, "Enter", 844, 182, 112, 50),
    (86, "1", 1150, 234, 50, 50), (87, "2", 1202, 234, 50, 50), (88, "3", 1254, 234, 50, 50),
    (89, "M10", 53, 286, 50, 50), (90, "M1", 1, 78, 50, 50), (91, "Ctrl", 122, 286, 61, 50),
    (92, "Win", 185, 286, 61, 50), (93, "Alt", 248, 286, 61, 50), (94, "Space", 311, 286, 183, 50),
    (95, "Alt", 706, 286, 61, 50), (96, "Fn", 769, 286, 61, 50), (97, "App", 832, 286, 61, 50),
    (98, "Ctrl", 895, 286, 61, 50), (99, "Lft", 975, 286, 50, 50), (100, "Dn", 1027, 286, 50, 50),
    (101, "Up", 1027, 234, 50, 50), (102, "Rgt", 1079, 286, 50, 50), (103, "Bksp", 855, 78, 101, 50),
    (104, "0", 1150, 286, 102, 50), (105, ".", 1254, 286, 50, 50), (106, "Enter", 1306, 234, 50, 102),
    (109, "Space", 519, 286, 185, 50), (112, "PrtSc", 975, 1, 50, 50), (113, "ScrLk", 1027, 1, 50, 50),
    (115, "Pause", 1079, 1, 50, 50), (116, "Ins", 975, 78, 50, 50), (117, "Home", 1027, 78, 50, 50),
    (118, "PgUp", 1079, 78, 50, 50), (119, "Del", 975, 130, 50, 50), (120, "End", 1027, 130, 50, 50),
    (121, "PgDn", 1079, 130, 50, 50), (122, "-", 1306, 78, 50, 50), (123, "+", 1306, 130, 50, 102),
]

# ── side strips (S1–S29) ───────────────────────────────────────────────────────
# ── per-key region geometry ───────────────────────────────────────────────────
# Where the host's per-key entries live inside the 1152-byte config, and how
# many of them there are. This is firmware-defined, not a free choice: it is
# derived so that the region ends exactly at ACTIVE_BLOCK.
#
# firmware.strip28.bin reads 146 entries from 0x1F8. Every earlier image --
# firmware.bin.m10 and firmware.renderfix.bin included -- reads 144 entries
# from 0x200, so a 1152-byte config or GUI cache carried over from one of those
# has to be rotated or each key is lit with its neighbour's colour. See
# _fit_key_region.
EFFECT_REGION, ACTIVE_BLOCK = 0x000, 0x440
KEY_COUNT = 146
KEY_REGION = ACTIVE_BLOCK - KEY_COUNT * 4      # 0x1F8
OLD_KEY_COUNT = 144                            # every pre-strip28 image
OLD_KEY_REGION = ACTIVE_BLOCK - OLD_KEY_COUNT * 4   # 0x200
PAYLOAD_LEN = 1152
ENTRY = 4                                     # one per-key entry = [0x80,R,G,B]


def _fit_key_region(buf, src):
    """Rotate a 1152-byte config from the layout at `src` to the 146-entry one.

    `src` is required, and is the offset the buffer's per-key region actually
    starts at. Passing OLD_KEY_REGION rotates a pre-strip28 image; passing
    KEY_REGION is a no-op, so it is safe to call on a buffer of unknown vintage.
    Getting it wrong is not a no-op -- it shifts every colour by two entries --
    which is why there is no default.

    Entries 144/145 are new (side-light channels 146/147), so the whole region
    has to start 8 bytes earlier for it to fit before the active block. The 8
    bytes that frees up come out of the documented zero padding at 0x140-0x1FF.
    Nothing ever read that padding, so it carries no colour worth keeping and
    the two new slots are left zero: not armed, which is the right state for two
    LEDs that were unreachable a build ago.

    Returns `buf` modified in place.
    """
    if src == KEY_REGION:
        return buf
    assert len(buf) == PAYLOAD_LEN, f"not a 1152-byte config: {len(buf)}"
    assert src > KEY_REGION, "only ever grows the region"
    grown = KEY_COUNT - OLD_KEY_COUNT          # 2 new entries
    assert src - KEY_REGION == grown * ENTRY, "region moved by the wrong amount"
    keys = bytes(buf[src:ACTIVE_BLOCK])        # the old 144 entries
    # keys first, then the new entries: padding first would slide the whole
    # region up by two.
    new = keys + bytes(grown * ENTRY)
    assert len(new) == ACTIVE_BLOCK - KEY_REGION
    buf[KEY_REGION:ACTIVE_BLOCK] = new
    assert len(buf) == PAYLOAD_LEN, "rotation resized the buffer"
    return buf


# The two silkscreened side-strip runs, drawn as their own light targets rather
# than as keys. A strip is painted through exactly the same per-key region as a
# key: firmware's `c0c6` scatters entry i to channel REMAP[i] and sets
# mask/plane for it. On firmware.strip28.bin 29 otherwise-unused entries route
# into channels 119-147, and LAYOUT draws none of them, so adding strips costs
# no key its colour.
#
# The channel -> entry direction is read out of the remap table. On
# firmware.renderfix.bin that table is 144 bytes at 0x6EEB and is a PERMUTATION,
# not the identity: ascending channels 119..145 do NOT map to ascending entries.
# firmware.strip28.bin relocates it to 0xE900 and appends entries 144 and 145,
# which are the only route to channels 146 and 147 -- the two LEDs past S27, which
# the OEM app leaves dark under every stock image. See docs/KEYMAP.md.
#
# S-number -> physical LED is still PROVISIONAL. Channel -> LED is uncalibrated
# (the transmit netlist at 0x7764 is not injective, and 27 channels drive fewer
# LEDs than they have channels), so "S1" means "the lowest strip channel", NOT
# "LED1". Once the one-entry-at-a-time calibration pass has been run, edit
# _STRIP_ENTRY and the two X constants below; nothing else needs to change.
_STRIP_ENTRY = {
    119: 124, 120: 125, 121: 126, 122: 127, 123: 128, 124: 129, 125: 130,
    126: 131, 127: 132, 128: 133, 129: 134, 130: 135, 131: 136, 132: 137,
    133: 138, 134: 139, 135: 140, 136: 141, 137: 142, 138: 143, 139: 114,
    140: 108, 141: 107, 142: 16, 143: 15, 144: 14, 145: 0,
    146: 144, 147: 145,                          # strip28 only: two new entries
}
# Strip runs follow the physical board: S1-S4 along the top of the macro block,
# S5-S14 down the left edge, S15-S18 along the top of the numpad, S19-S29 down
# the right edge. Each group is (axis, first S, count, x, y, pitch) and groups
# are laid out in S order, which is also ascending channel order.
_STRIP_CELL, _STRIP_PITCH, _STRIP_TOP = 19, 22, 28
_STRIP_GROUPS = (
    ("h", 1,   4, 1,    _STRIP_TOP, _STRIP_PITCH),  # S1-S4   above the macros
    ("v", 5,  10, -26,  _STRIP_TOP, _STRIP_PITCH),  # S5-S14  left edge
    ("h", 15,  4, 1150, _STRIP_TOP, _STRIP_PITCH),  # S15-S18 above the numpad
    ("v", 19, 11, 1382, _STRIP_TOP, _STRIP_PITCH),  # S19-S29 right edge
)
# The left edge column needs space the board does not occupy, so the design
# space starts at a negative x and CANVAS_W grows to span it. The paint and
# hit-test origin is offset by CANVAS_MIN_X so key coordinates are unchanged.
CANVAS_MIN_X = -26
CANVAS_W = 1382 + _STRIP_CELL - CANVAS_MIN_X + 3
CANVAS_H = 338


def _build_strips():
    chans = sorted(_STRIP_ENTRY)
    pos = [(gx + (k * _STRIP_PITCH if axis == "h" else 0),
            gy + (0 if axis == "h" else k * _STRIP_PITCH))
           for (axis, _first, count, gx, gy, _p) in _STRIP_GROUPS
           for k in range(count)]
    assert len(pos) == len(chans), "strip groups must cover every channel"
    return tuple((f"S{i + 1}", _STRIP_ENTRY[c], c, pos[i][0], pos[i][1],
                  _STRIP_CELL, _STRIP_CELL)
                 for i, c in enumerate(chans))


STRIPS = _build_strips()                      # (name, entry, channel, x, y, w, h)
STRIP_ENTRIES = tuple(s[1] for s in STRIPS)
_STRIP_BY_ENTRY = {s[1]: (s[0], s[2]) for s in STRIPS}
assert len(STRIP_ENTRIES) == 29 == len(set(STRIP_ENTRIES))
assert max(STRIP_ENTRIES) < KEY_COUNT, "a strip entry is outside the per-key region"
assert not (set(STRIP_ENTRIES) & {i for i, *_ in LAYOUT}), \
    "a strip entry collides with a key entry"

# The ten macro keys are labelled M1-M10 following the convention already
# recorded in docs/KEYMAP.md: the outer (x=1) column is M1-M5 top to bottom and
# the inner (x=53) column is M6-M10, which keeps M10 = Light (idx 89) as the
# docs and firmware notes all assume. The OEM function names each label
# replaces are kept here so the hover tooltip does not lose them.
MACRO_ENTRIES = (90, 18, 36, 54, 72, 17, 35, 53, 71, 89)
_MACRO_ALIAS = {90: "SelAll", 18: "Copy", 36: "Paste", 54: "Cut", 72: "Fn",
                17: "M1", 35: "M2", 53: "M3", 71: "M4", 89: "Light"}
assert set(MACRO_ENTRIES) == set(_MACRO_ALIAS)
assert {i for i, *_ in LAYOUT if 1 <= i <= 90} >= set(MACRO_ENTRIES)

# Keys that can be picked as a remap *target*. Derived from HID_TARGETS rather
# than listed by hand: a key is pickable exactly when the board can report a HID
# usage for it that the encoder would accept. That excludes the ten macro keys
# and the M1-M10 column, which have no usage, so the target picker can dim them
# instead of offering a target that encodes to nothing.
_MAPPABLE_USAGES = frozenset(u for u, _lbl in HID_TARGETS)
_MAPPABLE_KEYS = frozenset(
    _i for _i, _n, *_r in LAYOUT
    if KM_KEYS.get(_i, (0, None))[0] in _MAPPABLE_USAGES)
# The usages the board itself can be clicked for. The target picker offers all
# of these, so the HID catalog never has to be listed in a widget. Three
# catalog usages fall outside it because this keyboard has no key for them.
_BOARD_USAGES = frozenset(
    KM_KEYS[i][0] for i in _MAPPABLE_KEYS if KM_KEYS[i][0] in _MAPPABLE_USAGES)


def _light_name(idx):
    """Short label for a light target: "S7" for a strip, "key 42" for a key."""
    if idx in _STRIP_BY_ENTRY:
        return _STRIP_BY_ENTRY[idx][0]
    return f"key {idx}"

# ── lighting zones (per-key paint groups) ─────────────────────────────────────
# Each zone is a list of light_index values from LAYOUT. The C989 is a split
# board: the w g between the halves runs between B/5/T/G (x≈470) and Y/6/H/7
# (x≈520), so halves split on key x-centre < 500.
ZONE_SPLIT = 500


def _split_halves():
    left, right = [], []
    for i, _name, x, _y, w, _h in LAYOUT:
        (left if x + w / 2 < ZONE_SPLIT else right).append(i)
    return left, right


ZONES = (
    # "All" deliberately includes the strips. Excluding them reproduces the
    # worst failure mode in this project: "I painted everything and the side
    # lights stayed dark" with no visible reason.
    ("All", [i for i, *_ in LAYOUT] + list(STRIP_ENTRIES)),
    # WASD/QWERDF stay upper-case: they name key groups, and title-casing them
    # would read as "Wasd"/"Qwerdf".
    ("WASD", [39, 56, 57, 58]),                # W A S D
    ("QWERDF", [38, 39, 40, 41, 58, 59]),      # Q W E R D F
    ("0-9", [20, 21, 22, 23, 24, 25, 26, 27, 28, 29]),
    ("Numpad", [32, 33, 34, 50, 51, 52, 68, 69, 70, 86, 87, 88, 104, 105, 106, 122, 123]),
    ("Macro Keys", list(MACRO_ENTRIES)),         # M1-M10, both columns
    ("Arrow Keys", [99, 100, 101, 102]),
    ("Insert Group", [116, 117, 118, 119, 120, 121]),
    ("F Keys", [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]),
    ("Side Strips", list(STRIP_ENTRIES)),
    ("Left Keyboard", _split_halves()[0]),
    ("Right Keyboard", _split_halves()[1]),
)
# Zone list lays out two columns of _ZONE_COLS; 11 entries => 6 + 5.
_ZONE_COLS = 2
_ZONE_ROWS = -(-len(ZONES) // _ZONE_COLS)      # ceil: 6
# Matches the effect list's 18pt. Only affordable because the key counts moved
# into the item tooltips: with "(N keys)" in the labels this column tops out at
# 13pt inside the 1487px window (18pt would need 1609). 22pt would also fit if
# the zone names ever get shorter still.
_ZONE_FONT_PT = 18

EFFECTS = [
    (0x01, "Static Color"), (0x02, "Glitter"), (0x03, "Colorful"), (0x04, "Single Off"),
    (0x05, "Breath"), (0x06, "Spectrum"), (0x07, "Scrolling"), (0x08, "Falling"),
    (0x09, "Ripple"), (0x0A, "Pinwheel"), (0x0E, "Shuttle"),
    (0x0F, "Sine Wave"), (0x10, "Diagonal"), (0x13, "Random"),
]

USER_ID = cloudnine_app.USER_ID
# "User Defined 2" is a GUI-only second per-key palette, not a device mode: the
# firmware's per-key twin 0x14 is rejected by the config loader and all 20 effect
# slots are taken, so both entries drive the one 0x80 per-key region. The parked
# palette lives in the GUI cache; 0x81 is a display-only stand-in that _dev_id()
# folds back to 0x80 before anything reaches the payload.
USER2_ID = 0x81
USER_IDS = (USER_ID, USER2_ID)

# Display order, which is deliberately not id order: "User Defined" breaks into
# the run after Scrolling and "User Defined 2" closes the list, which is what
# swaps them past Random and Falling. 16 entries, two columns of 8.
EFFECT_LIST = (list(EFFECTS[:7])
               + [(USER_ID, "User Defined"), EFFECTS[13]]
               + list(EFFECTS[7:13])
               + [(USER2_ID, "User Defined 2")])
assert len(EFFECT_LIST) == 16 and len(dict(EFFECT_LIST)) == 16, "effect list clash"
assert sorted(e for e, _ in EFFECT_LIST) == sorted(
    [e for e, _ in EFFECTS] + list(USER_IDS)), "effect list must cover every id"


def _dev_id(eid):
    """Map the GUI-only second-palette id onto the device's 0x80 per-key mode."""
    return USER_ID if eid == USER2_ID else eid


# Effect-list font, in points. 18 is the largest that still fits two columns of
# 8 inside the Effects column's ~410px share without visibly squeezing the other
# panels (it costs Parameters 2px). 19+ takes 16-44px from Parameters and buys
# no extra keyboard-preview height, which is already at its 212px floor here.
_EFFECT_FONT_PT = 18

# Brightness (effect-slot byte [9]) is an INDEX into a firmware table, not a
# 0-255 level -- see docs/KEYMAP.md (brightness table) and docs/investigation.md
# for the retracted claims. There are two tables
# and the steady state always uses 0x6f7b: 0x6f8b is only reachable while
# b0==0xff (the transient config-load path), and the mode table at 0x647c only
# ever restores b0 to 0x00/0x07, never 0xff (doc 4). Table 0x6f7b reads:
#
#   idx   0    1   2   3   4   5   6   7   8   9   a   b   c   d   e   f
#   val  00   00  20  40  60  90  c0  ff  00  00  02  04  06  08  08  08
#
# so indices 8 and 9 are literally 0x00 and anything >= 0x10 is past the end of
# the table. Worse, bri_val == 0 is not a usable "off": with bri_val 0 the board
# goes black *and* the board reports a fault state (confirmed live 2026-09-27 -
# some light turns green, and it clears at 25%+). NOTE: that green was recorded
# as "the side strips", but with the wheel now known to be separate hardware the
# attribution is unverified - it may have been the wheel ring. Indices
# 0x00, 0x01, 0x08
# and 0x09 all resolve to 0x00, so none of them are offered. The UI used to be a
# raw 0-255 slider, which meant 219 of its 256 positions sent the firmware a
# brightness it reads as black or as out-of-table garbage. The slider is now a
# stepper over the six indices that actually light the board.
_BRI_INDEX = (0x02, 0x03, 0x04, 0x05, 0x06, 0x07)
_BRI_VALUE = (0x20, 0x40, 0x60, 0x90, 0xC0, 0xFF)
_BRI_STEP_OF = dict(zip(_BRI_INDEX, range(len(_BRI_INDEX))))
_BRI_DEFAULT = 4  # step for 0x06, the OEM factory brightness
# 20 effect slots: 0..18 hold ids 0x01..0x13, slot 19 holds 0x80.
_SLOT_COUNT = 20

# Speed (byte [10]) has the same shape of bug: FUN_0000ab14 does
# `ldrb r0,[0x6F9C + idx]` with the raw config byte and no clamp either. The
# value it picks is used as `period = (ec * c9) / 400`, so a LARGER c9 is a
# SLOWER animation. Table 0x6f9c is 8 entries; indices past 7 read into the
# solid-path table at 0x6fa4 and then unrelated code.
#
#   idx   0    1    2    3    4    5    6    7
#   c9   ff   ff   c8   96   64   4b   28   28
#
# 0/1, 6/7 and so on are duplicates, so the UI steps over the six distinct
# periods rather than showing eight positions that do the same thing.
_SPD_INDEX = (0x00, 0x02, 0x03, 0x04, 0x05, 0x06)
_SPD_PERIOD = (0xFF, 0xC8, 0x96, 0x64, 0x4B, 0x28)
_SPD_STEP_OF = dict(zip(_SPD_INDEX, range(len(_SPD_INDEX))))
_SPD_DEFAULT = 5  # step for index 0x06, the OEM factory speed


def _spd_step(raw):
    """Step for a raw speed byte; anything out of range falls back to OEM."""
    return _SPD_STEP_OF.get(raw, _SPD_DEFAULT)


def _bri_step(raw):
    """Nearest usable step for a raw brightness byte (also the legacy repair).

    Never returns a step whose value is 0 -- that is the firmware's fault state,
    not an off setting, so a config carrying one is lifted to the OEM default
    rather than to the nearest dim step.
    """
    if raw in _BRI_STEP_OF:
        return _BRI_STEP_OF[raw]
    best = min(range(len(_BRI_VALUE)), key=lambda s: abs(_BRI_VALUE[s] - raw))
    return best if _BRI_VALUE[best] else _BRI_DEFAULT


def _bri_pct(step):
    return round(_BRI_VALUE[step] * 100 / 0xFF)


_POS = {}
for _i, _n, _x, _y, _w, _h in LAYOUT:
    _POS[_i] = (int((_x + _w / 2) // 68), int((_y + _h / 2) // 50))
_KEY_IDS = tuple(_POS)
_ROW_DENSE = {r: i for i, r in enumerate(sorted({p[1] for p in _POS.values()}))}
_COL_DENSE = {c: i for i, c in enumerate(sorted({p[0] for p in _POS.values()}))}
_COL_SPLIT = 7
# Diagonals for 0x10, keyed on row - col so they run down-right. The leading
# band starts on {M5} and grows through {M4,M10} then {M3,M9,Ctrl} as it enters
# the board. The pattern repeats every _DIAG_PERIOD diagonals, so several bands
# are on screen at once and the whole set shifts one diagonal per step.
_DIAG = {}
for _k, (_c, _r) in _POS.items():
    _DIAG.setdefault(_ROW_DENSE.get(_r, 0) - _COL_DENSE.get(_c, 0), []).append(_k)
_DIAG_MAX = max(_DIAG)
_DIAG_PERIOD = 4
_DIAG_SEQ = tuple(frozenset(
    _k for _d, _g in _DIAG.items()
    if (_d - (_DIAG_MAX - _s)) % _DIAG_PERIOD == 0 for _k in _g)
    for _s in range(_DIAG_PERIOD))
_DIAG_STEP = 3
# On any beat where "m3" (XML Paste) is lit, "m5" (XML Fn) lights with it.
_DIAG_TRIG, _DIAG_ECHO = 36, 72
_DIAG_EXTRA = tuple((_DIAG_ECHO,) if _DIAG_TRIG in g else () for g in _DIAG_SEQ)
# Effects whose colour does not vary from key to key. "Full spectrum" only
# makes sense for these; anywhere else it would flatten the pattern, so the
# override is restricted to them and every other effect is immune by default.
_UNIFORM_EFFECTS = frozenset((0x01, 0x05))
_EFFECT_IDS = frozenset(eid for eid, _ in EFFECTS)
assert _UNIFORM_EFFECTS <= _EFFECT_IDS, "unknown effect id in _UNIFORM_EFFECTS"
_SHUTTLE_W = 4
_SHUTTLE_STEP = 2
_ROW_SEQ = []
for _r in sorted({p[1] for p in _POS.values()}):
    _members = [k for k in _POS if _POS[k][1] == _r]
    _ROW_SEQ.append(tuple(k for _, k in sorted((_POS[k][0], k) for k in _members)))
_SHUTTLE_TOTAL = sum(max(1, len(s) - _SHUTTLE_W + 1) for s in _ROW_SEQ)

PALETTE = ["#ffffff", "#00ffff", "#ff0000", "#ffff00",
           "#0000ff", "#ff0080", "#ff8000", "#00ff00"]

def load_default_buf():
    """The OEM factory config, rotated onto the current 146-entry layout.

    Every shipped image was captured with the 144-entry region at 0x200, so the
    rotation is unconditional here -- there is no such thing as a pre-rotated
    default file.
    """
    for path in DEFAULT_CANDIDATES:
        try:
            data = open(path, "rb").read()
            if len(data) == 1152:
                return _fit_key_region(bytearray(data), OLD_KEY_REGION)
        except OSError:
            continue
    buf = bytearray(default_template())
    # make slot 15 (id 0x0f) the active effect like the OEM factory config
    buf[ACTIVE_BLOCK:ACTIVE_BLOCK + 16] = buf[14 * 16:15 * 16]
    return _fit_key_region(buf, OLD_KEY_REGION)


def load_default(name, size):
    """Load a captured OEM page of `size` bytes.

    Raises if the file is missing -- it deliberately does NOT fall back to zeros.
    A zero page is not a harmless placeholder for these: in the wheel page the
    first byte of each of the 4 rows is the smart-wheel FUNCTION bound to that
    detent, so a blank page silently rebinds all four detents to function 0 and
    the ring stops answering the mode its detent is set to. That is exactly what
    happened when the app ran from a tree with no data/ directory (2026-09-29):
    the write was accepted by the device, and the GUI then reported a
    successful change that had actually destroyed the wheel's function map.
    """
    path = asset_path("data", "oem_defaults", name)
    with open(path, "rb") as f:
        data = f.read()
    buf = bytearray(size)
    buf[:min(len(data), size)] = data[:size]
    return buf


# Local state persistence.
#
# The C989 cannot actually read its own lighting/keymap config back: the
# read-opcodes (0x0402/0x0412/0x0411/0x0405) reply with zero-filled pages
# (verified live 2026-09-18). So to make the GUI show "the lighting that's on
# the device", we persist the last applied/saved buffers here and restore them
# at startup, then attempt a best-effort live read (harmless if zero-filled).
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".config", "cloudnine-c989")
# Both per-key caches carry the entry layout in their filename, because a 1152-
# byte colours cache and a 2x584-byte palette file are indistinguishable from
# their 144-entry predecessors by size alone -- and reading a stale one would
# silently light every key with its neighbour's colour. The .bin names are the
# pre-strip28 format and are read once, then rotated (see _fit_key_region).
#
# The old files are deliberately NOT deleted after rotation. They are still a
# valid 144-entry config for an older build, and an older build reading the new
# k146 file would be just as wrong in the opposite direction. Leaving both means
# each build finds the file it can read.
COLORS_CACHE = os.path.join(CACHE_DIR, f"colors.last.k{KEY_COUNT}.bin")
COLORS_CACHE_OLD = os.path.join(CACHE_DIR, "colors.last.bin")
KEYMAP_CACHE = os.path.join(CACHE_DIR, "keymap.last.bin")
CUSTOM_COLORS_FILE = os.path.join(CACHE_DIR, "custom_colors.bin")
# Parked per-key palette for "User defined 2": two per-key regions
# (KEY_REGION..ACTIVE_BLOCK), palette 0 first.
PALETTES_FILE = os.path.join(CACHE_DIR, f"per_key_palettes.k{KEY_COUNT}.bin")
PALETTES_FILE_OLD = os.path.join(CACHE_DIR, "per_key_palettes.bin")
# The wheel page (0x0425, flash 0xA400) is a separate 64-byte region,
# not part of the 1152-byte config, so it needs its own cache. Without
# one the Smart wheel tab re-seeds from the OEM default at every
# launch, and an Apply then writes all four detents from that default
# buffer - so changing one setting silently reset the other three.
WHEEL_CACHE = os.path.join(CACHE_DIR, "wheel.last.bin")

# The macro library. The keyboard cannot be read back (0x0414 returns zeros on
# this firmware), so the app is the only place a recorded macro survives an app
# restart -- and an upload replaces every macro the keyboard holds, so anything
# not saved here is gone. JSON rather than a bare region file so the names are
# in it and it stays readable.
def macro_library_path():
    """Where the macro library is kept.

    Computed on each call, not once at import: the tests redirect CACHE_DIR to
    a temp directory *after* importing this module, and a constant captured at
    import time would have every test read and write the real
    ~/.config/cloudnine-c989/macros.json.
    """
    return os.path.join(CACHE_DIR, "macros.json")


def macro_upload_stamp_path():
    """Where the record of the last macro upload is kept.

    The keyboard cannot be read back -- `0x0414` answers with zeros on this
    firmware, verified again 2026-10-10 -- so nothing on this side can tell what
    the device actually holds. That gap caused real confusion: an encoding fix
    was made, the key was pressed without re-uploading, and the old bytes played
    exactly as before, which looked like the fix having failed.

    So each successful upload leaves a fingerprint here, and
    `tools/watch_macro.py` compares it against the region the current code would
    build. A mismatch is reported before anything is pressed, which is the only
    place the check is useful.
    """
    return os.path.join(CACHE_DIR, "last_macro_upload.json")


def macro_region_fingerprint(events) -> dict:
    """What this app last wrote, in terms that matter for playback.

    Deliberately not a hash of the bytes: the readable fields are what a person
    needs to see when a fix does not seem to have taken -- the per-slot record
    counts, and the block length field, which is the one that was wrong.
    """
    region = build_macro_region([m["events"] for m in events]) \
        if events and isinstance(events[0], dict) \
        else build_macro_region(events)
    ptr = lambda i: int.from_bytes(region[i * 4:i * 4 + 4], "little")
    counts = [len(m["events"] if isinstance(m, dict) else m) for m in events]
    return {
        "region_bytes": len(region),
        "slot_records": counts,
        # The length field the firmware reads, per slot. 2n-1.
        "block_headers": [macro_block_header(n) for n in counts],
        "slot_pointers": [hex(ptr(i)) for i in range(len(counts) + 1)],
        "end_markers": sum(1 for i in range(len(region) - 3)
                           if region[i:i + 4] == MACRO_SENTINEL),
    }

# A slot's index is a byte in the action word, so the library cannot grow past
# 256 entries. Fifteen matches the spin box the key editor has always used.
MACRO_SLOT_LIMIT = 15
REGION_LEN = ACTIVE_BLOCK - KEY_REGION
OLD_REGION_LEN = ACTIVE_BLOCK - OLD_KEY_REGION
CUSTOM_DEFAULTS = ["#ff0000", "#00ff00", "#0000ff", "#ffff00",
                   "#ff00ff", "#00ffff", "#ff8000", "#808080"]


def read_cache(path, size):
    try:
        data = open(path, "rb").read()
        return data if len(data) == size else None
    except OSError:
        return None


def write_cache(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    except OSError:
        pass


class LightState:
    """Mutable 1152-byte color/effect config plus accessors."""

    def __init__(self):
        self.buf = load_default_buf()
        # Set by MainWindow: called after any per-key region edit so the active
        # palette is persisted. Routing it through the mutators means a new
        # edit path cannot silently skip the save.
        self.on_perkey_change = None
        self.param_repaired = self._normalize_params()

    def _normalize_params(self):
        """Snap every slot's brightness and speed bytes to usable indices.

        Bytes [9] and [10] are both firmware *table indices* with no bounds
        check on read (see _BRI_INDEX / _SPD_INDEX), so a raw 0-255 value is
        not "full brightness" - it indexes past the end of the table and picks
        up unrelated bytes. A brightness byte that lands on 0x00 blacks out the
        whole board. Configs written by an older build carry such values, and
        the damage only shows once that effect is *activated*, long after the
        file was written, so repair on load. Returns the number of bytes fixed.
        """
        fixed = 0
        for base in (0, ACTIVE_BLOCK):
            for s in range(_SLOT_COUNT) if base == 0 else (None,):
                o = base + (self.slot_off(s) if s is not None else 0)
                if o + 10 >= len(self.buf):
                    break
                for off, step_of, table in ((9, _bri_step, _BRI_INDEX),
                                            (10, _spd_step, _SPD_INDEX)):
                    step = step_of(self.buf[o + off])
                    if self.buf[o + off] != table[step]:
                        self.buf[o + off] = table[step]
                        fixed += 1
        return fixed

    def _touch_perkey(self):
        if self.on_perkey_change is not None:
            self.on_perkey_change()

    # -- effect slots --------------------------------------------------------
    def slot_off(self, slot):
        return slot * 16

    def slot(self, slot):
        o = self.slot_off(slot)
        return dict(id=self.buf[o], r=self.buf[o + 1], g=self.buf[o + 2],
                    b=self.buf[o + 3], fullcolor=self.buf[o + 8],
                    bri=self.buf[o + 9], spd=self.buf[o + 10], dir=self.buf[o + 11])

    def set_slot(self, slot, **kw):
        o = self.slot_off(slot)
        f = {"id": 0, "r": 0, "g": 0, "b": 0, "fullcolor": 0,
             "bri": 6, "spd": 6, "dir": 0}
        f.update(self.slot(slot))
        f.update(kw)
        self.buf[o + 0] = f["id"] & 0xFF
        self.buf[o + 1] = f["r"] & 0xFF
        self.buf[o + 2] = f["g"] & 0xFF
        self.buf[o + 3] = f["b"] & 0xFF
        self.buf[o + 4:o + 8] = b"\x00" * 4
        self.buf[o + 8] = f["fullcolor"] & 0xFF
        self.buf[o + 9] = f["bri"] & 0xFF
        self.buf[o + 10] = f["spd"] & 0xFF
        self.buf[o + 11] = f["dir"] & 0xFF
        self.buf[o + 12:o + 14] = b"\x00\x00"
        self.buf[o + 14:o + 16] = b"\xAA\x55"

    def slot_for_id(self, eid):
        # Normalise first: 0x81 must land on the 0x80 slot, not on slot 128.
        eid = _dev_id(eid)
        return 19 if eid == USER_ID else eid - 1

    def active(self):
        o = ACTIVE_BLOCK
        return dict(id=self.buf[o], r=self.buf[o + 1], g=self.buf[o + 2],
                    b=self.buf[o + 3], fullcolor=self.buf[o + 8],
                    bri=self.buf[o + 9], spd=self.buf[o + 10], dir=self.buf[o + 11])

    def activate(self, eid):
        """Make effect `eid` active: copy its slot into the active block.

        Folds the GUI-only second-palette id back to 0x80 first, so the slot
        and the active block never see 0x81.
        """
        eid = _dev_id(eid)
        slot = self.slot_for_id(eid)
        self.set_slot(slot, id=eid)
        self.buf[ACTIVE_BLOCK:ACTIVE_BLOCK + 16] = \
            self.buf[self.slot_off(slot):self.slot_off(slot) + 16]

    def set_active(self, **kw):
        f = self.active()
        f.update(kw)
        o = ACTIVE_BLOCK
        self.buf[o] = f["id"] & 0xFF
        self.buf[o + 1] = f["r"] & 0xFF
        self.buf[o + 2] = f["g"] & 0xFF
        self.buf[o + 3] = f["b"] & 0xFF
        self.buf[o + 8] = f["fullcolor"] & 0xFF
        self.buf[o + 9] = f["bri"] & 0xFF
        self.buf[o + 10] = f["spd"] & 0xFF
        self.buf[o + 11] = f["dir"] & 0xFF
        # keep the source slot in sync
        slot = self.slot_for_id(f["id"])
        self.set_slot(slot, id=f["id"], r=f["r"], g=f["g"], b=f["b"],
                      fullcolor=f["fullcolor"], bri=f["bri"], spd=f["spd"], dir=f["dir"])

    # -- per-key region ------------------------------------------------------
    def key_color(self, idx):
        """Return (r,g,b) if the entry is enabled (byte0==0x80), else None."""
        o = KEY_REGION + idx * 4
        if self.buf[o] == 0x80:
            return (self.buf[o + 1], self.buf[o + 2], self.buf[o + 3])
        return None

    def set_key(self, idx, r, g, b):
        o = KEY_REGION + idx * 4
        self.buf[o] = 0x80
        self.buf[o + 1] = r & 0xFF
        self.buf[o + 2] = g & 0xFF
        self.buf[o + 3] = b & 0xFF
        self._touch_perkey()

    def clear_key(self, idx):
        o = KEY_REGION + idx * 4
        self.buf[o:o + 4] = b"\x00\x00\x00\x00"
        self._touch_perkey()

    def clear_all_keys(self):
        self.buf[KEY_REGION:ACTIVE_BLOCK] = b"\x00" * (ACTIVE_BLOCK - KEY_REGION)
        self._touch_perkey()


class KeymapState:
    """Mutable 640-byte key remap table plus accessors.

    Entry layout (see docs/KEYMAP.md): `[02 00 <HID usage> 00]` for a remap,
    `00 00 00 00` for pass-through, entry 159 = `00 00 aa 55` sentinel.
    """

    def __init__(self):
        self.buf = bytearray(km_default())
        self.selected = None
        # Which of the two firmware layers this buffer edits ("top"/"fn").
        self.layer = LAYER_TOP

    def entry(self, idx):
        o = idx * 4
        if 0 <= o < len(self.buf):
            return self.buf[o:o + 4]
        return None

    def is_remapped(self, idx):
        """True when this key has a mapping of any kind.

        The canvas legend says turquoise means "remapped key", so this has to
        cover every mode that writes a word -- not just a plain single-key
        remap. Matching the plain-remap prefix alone left combos, mouse,
        media, hotkey, macro and disabled keys all showing as untouched, which
        is six of the eight working modes.

        Decided by whether the word is one we can read: an unrecognised word
        stays unturquoise rather than claiming a mapping we cannot describe.
        """
        w = self.entry(idx)
        if not w or w == b"\x00\x00\x00\x00":
            return False
        return describe_word(w) != ""

    def target_usage(self, idx):
        w = self.entry(idx)
        if w and w[0] == 0x02:
            return w[2]
        return None

    def target_mods(self, idx):
        """Modifier bitmap B1 for a remap/combo entry, 0 if none."""
        w = self.entry(idx)
        if w and w[0] == 0x02:
            return w[1]
        return 0

    def set_remap(self, idx, usage, mods=0):
        return self.set_word(idx, combo_word(usage, mods))

    def set_word(self, idx, word):
        """Store a whole 4-byte action word. Guards the slot, not the value.

        The value is the caller's to get right -- the action types that are
        decoded have their own `*_word()` builders -- so the guard here is only
        about writing somewhere valid.
        """
        if idx in KM_UNMAPPABLE or idx < 0 or idx >= 160 or idx == 159:
            return False
        self.buf[idx * 4:idx * 4 + 4] = word
        return True

    def describe(self, idx):
        """What this key currently does, or '' if the word is not understood."""
        return describe_word(self.entry(idx) or b"\x00\x00\x00\x00")

    def clear(self, idx):
        if idx >= 0 and idx < 160 and idx != 159:
            self.buf[idx * 4:idx * 4 + 4] = b"\x00\x00\x00\x00"

    def clear_all(self):
        # In place, not a rebind: the GUI holds a second reference to this
        # bytearray as the live view of the active layer, and rebinding would
        # leave that view pointing at the old buffer.
        #
        # Per layer, not km_default(): the Fn layer's factory state is NOT the
        # Top layer's keymap -- it ships 94 keys disabled (fn_default_payload).
        self.buf[:] = km_layer_default(self.layer)

    def key_color(self, idx):
        """For the canvas: (r,g,b) turquoise for remapped keys."""
        if self.is_remapped(idx):
            return (64, 255, 200)
        return None

    # KeyboardCanvas also consults active()/key_color on the light side; the
    # remap canvas only needs key_color(), kept duck-typed here.

    def active(self):
        return {"id": USER_ID}


class KeyboardCanvas(QWidget):
    keyClicked = pyqtSignal(int)
    keyRightClicked = pyqtSignal(int)
    # Emitted instead of keyClicked while this canvas is picking a *target*
    # usage, carrying the HID usage rather than the key entry index.
    usagePicked = pyqtSignal(int)

    def __init__(self, state, show_strips=True):
        super().__init__()
        self.state = state
        # The remap canvas reuses this widget but edits HID keymaps, and a
        # strip is a light target with no key behind it. Drawing them there
        # would let a click write a bogus remap for entry 124 into the keymap.
        self.show_strips = show_strips
        self.selected = None
        self.preview = (0, 0, 0)
        # {key_index: character} drawn over the board's own legends, so a
        # layout preset previews where it moves each cap before it is applied.
        # Empty means the board draws its US legends.
        self.legends = {}
        # When set, a left click picks a *target* HID usage for the selected
        # key instead of selecting a different source key. This is what
        # replaces the 110-entry target combo box: the board already shows
        # every key in place, so the physical key is the better thing to click
        # than an entry in a flat list.
        self.picking_target = False
        # The usage currently chosen as the target, drawn as a second outline.
        self.target_usage = None
        self.setMinimumSize(760, 210)
        # Strips are ~11px cells at the default scale, so hovering has to give
        # live feedback or they are unusable.
        self.setMouseTracking(True)
        self.sx = self.sy = 1.0
        self.ox = self.oy = 0.0
        # animated preview so picking an effect visibly moves the keyboard
        self.frame = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(60)

    def _tick(self):
        self.frame += 1
        self.update()

    def _key_color(self, idx, active, _frame):
        if active["id"] == USER_ID:
            c = self.state.key_color(idx)
            # An entry flagged 0x80 but painted 000000 is off on the board just
            # the same as an absent entry, so it gets the same flat board
            # colour. Without this the per-key path is the only one that can
            # paint a dark key as something other than #1c1c22.
            return QColor(*c) if c and any(c) else QColor("#1c1c22")
        r, g, b = active["r"], active["g"], active["b"]
        eid = active["id"]
        f = _frame
        if active["fullcolor"] and eid in _UNIFORM_EFFECTS:
            step = 6 if eid % 2 else -6
            rot = (eid * 40 + idx * step + f * 7) % 360
            return QColor.fromHsv(rot, 190, 225)
        if eid == 1:
            return QColor(r, g, b)
        if eid == 2:
            rot = ((idx * 117 + 17) % 231 + (f // 5) * 19 + idx * 7) % 360
            return QColor.fromHsv(rot, 190, 225)
        col, row = _POS.get(idx, (0, 0))
        if eid == 3:
            blink = (idx * 7919 + (f // 5) * 31) % 100 < 50
            return QColor(r, g, b) if blink else QColor("#1c1c22")
        if eid == 4:
            only = _KEY_IDS[((f // 12) * 37 + 11) % len(_KEY_IDS)]
            return QColor("#1c1c22") if idx == only else QColor(r, g, b)
        if eid == 5:
            br = 70 + int(185 * (0.5 + 0.5 * math.sin(f * 0.0942)))
            return QColor(r * br // 255, g * br // 255, b * br // 255)
        if eid == 6:
            rot = ((f // 8) * 15) % 360
            return QColor.fromHsv(rot, 190, 230)
        if eid == 7:
            rot = (((f // 8) + _COL_DENSE.get(col, 0)) * 15) % 360
            return QColor.fromHsv(rot, 190, 230)
        if eid == 8:
            rot = (((f // 8) - _ROW_DENSE.get(row, 0)) * 15) % 360
            return QColor.fromHsv(rot, 190, 230)
        if eid == 9:
            rot = (((f // 8) - _COL_DENSE.get(col, 0)) * 15) % 360
            return QColor.fromHsv(rot, 190, 230)
        if eid == 0x0A:
            d = _ROW_DENSE.get(row, 0)
            if _COL_DENSE.get(col, 0) < _COL_SPLIT:
                rot = (((f // 8) - d) * 15) % 360
            else:
                rot = (((f // 8) + d) * 15) % 360
            return QColor.fromHsv(rot, 190, 230)
        if eid == 0x0E:
            t = (f // _SHUTTLE_STEP) % _SHUTTLE_TOTAL
            for seq in _ROW_SEQ:
                span = max(1, len(seq) - _SHUTTLE_W + 1)
                if t < span:
                    win = seq[t:t + _SHUTTLE_W]
                    return QColor(r, g, b) if idx in win else QColor("#1c1c22")
                t -= span
            return QColor("#1c1c22")
        if eid == 0x0F:
            u = _COL_DENSE.get(col, 0) / max(1, len(_COL_DENSE) - 1)
            rot = (u * 360.0 + (f // 8) * 15) % 360
            br = 0.5 + 0.5 * math.sin(f * 0.12 - u * 3 * math.pi)
            return QColor.fromHsv(int(rot), 190, 60 + int(195 * br))
        if eid == 0x10:
            step = (f // _DIAG_STEP) % len(_DIAG_SEQ)
            band = _DIAG_SEQ[step]
            if idx in band or idx in _DIAG_EXTRA[step]:
                return QColor(r, g, b)
            return QColor("#1c1c22")
        if eid == 0x13:
            rot = (idx * 47 + 23) % 360
            return QColor.fromHsv(rot, 170, 235)
        return QColor(r, g, b)
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#101014"))
        sx = (self.width() - 16) / CANVAS_W
        sy = (self.height() - 16) / CANVAS_H
        s = min(sx, sy)
        self.sx = self.sy = s
        # CANVAS_MIN_X is negative (the left strip column), so the centring
        # offset has to be corrected by it or the whole board slides right.
        self.ox = (self.width() - CANVAS_W * s) / 2 - CANVAS_MIN_X * s
        self.oy = (self.height() - CANVAS_H * s) / 2

        active = self.state.active()

        for (idx, name, x, y, w, h) in LAYOUT:
            rx = self.ox + x * s
            ry = self.oy + y * s
            rw = w * s
            rh = h * s
            col = self._key_color(idx, active, self.frame)
            col = QColor(*col) if isinstance(col, tuple) else col
            p.setBrush(QBrush(col))
            if idx == self.selected:
                p.setPen(QPen(QColor("#ffffff"), 2))
            elif idx == self._target_key() and self.picking_target:
                # The key the target picker has landed on: a second, inner
                # outline so it reads as "this is where it will go" rather than
                # "this is the key you are editing".
                p.setPen(QPen(QColor("#40ffc8"), 2))
            elif self.picking_target and idx not in _MAPPABLE_KEYS:
                # Dimmed while picking, because a macro key or M1-M10 has no
                # HID usage and cannot be a target.
                p.setPen(QPen(QColor("#2a2a32"), 1))
            else:
                p.setPen(QPen(QColor("#3a3a44"), 1))
            p.drawRoundedRect(int(rx), int(ry), int(rw), int(rh), 4, 4)
            lum = 0.299 * col.red() + 0.587 * col.green() + 0.114 * col.blue()
            p.setPen(QColor("#000000") if lum > 128 else QColor("#d0d0d0"))
            fnt = QFont()
            fnt.setPointSize(max(6, int(6 * s)))
            p.setFont(fnt)
            p.drawText(int(rx), int(ry), int(rw), int(rh),
                       Qt.AlignmentFlag.AlignCenter,
                       self.legends.get(idx, name))

        # Side strips. Same colour path as a key, so in User Defined mode they
        # show the painted per-key colour and in an effect mode they show the
        # effect - which is what the hardware actually does.
        if not self.show_strips:
            return
        for (name, entry, _chan, x, y, w, h) in STRIPS:
            rx = self.ox + x * s
            ry = self.oy + y * s
            rw, rh = w * s, h * s
            col = self._key_color(entry, active, self.frame)
            col = QColor(*col) if isinstance(col, tuple) else col
            p.setBrush(QBrush(col))
            if entry == self.selected:
                p.setPen(QPen(QColor("#ffffff"), 2))
            else:
                p.setPen(QPen(QColor("#6b4f8f"), 1))
            p.drawRoundedRect(int(rx), int(ry), max(1, int(rw)),
                              max(1, int(rh)), 3, 3)

        # Group captions in widget units, so they stay legible at any scale.
        # The cells are far too small for an in-cell label at this scale. The
        # caption band is oy+2..oy+18, so it is only drawn when _STRIP_TOP * s
        # has grown past it; below that it is dropped rather than drawn over
        # the first row of cells.
        if _STRIP_TOP * s >= 18:
            capf = QFont()
            capf.setPointSize(8)
            p.setFont(capf)
            p.setPen(QColor("#8a8a99"))
            for (axis, first, count, gx, gy, pitch) in _STRIP_GROUPS:
                span = _STRIP_CELL if axis == "h" else (count - 1) * pitch + _STRIP_CELL
                cap = (f"S{first}" if count == 1
                       else f"S{first}-S{first + count - 1}")
                p.drawText(int(self.ox + gx * s), int(self.oy + 2),
                           max(1, int(span * s)), 16,
                           Qt.AlignmentFlag.AlignHCenter, cap)

    def _target_key(self):
        """The key entry whose HID usage is the currently picked target."""
        if self.target_usage is None:
            return None
        for idx, (_u, _n) in KM_KEYS.items():
            if _u == self.target_usage:
                return idx
        return None

    def _hit(self, pos):
        x = (pos.x() - self.ox) / self.sx
        y = (pos.y() - self.oy) / self.sy
        for (idx, name, kx, ky, kw, kh) in LAYOUT:
            if kx <= x <= kx + kw and ky <= y <= ky + kh:
                return idx
        for (_name, entry, _chan, kx, ky, kw, kh) in (STRIPS if self.show_strips else ()):
            if kx <= x <= kx + kw and ky <= y <= ky + kh:
                return entry
        return None

    def _target_tip(self, target):
        """Human label for a light target, or '' for empty space."""
        if target is None:
            return ""
        if target in _STRIP_BY_ENTRY:
            name, chan = _STRIP_BY_ENTRY[target]
            return f"{name} — strip channel {chan} (entry {target})"
        for (idx, name, _x, _y, _w, _h) in LAYOUT:
            if idx == target:
                # Show the same character the canvas drew, or the tooltip
                # would name one key while the board shows another.
                name = self.legends.get(idx, name)
                alias = _MACRO_ALIAS.get(idx)
                if alias:
                    return f"{name} — {alias} (key entry {idx})"
                return f"{name} — key entry {idx}"
        return ""

    def mouseMoveEvent(self, ev):
        self.setToolTip(self._target_tip(self._hit(ev.position())))

    def mousePressEvent(self, ev):
        idx = self._hit(ev.position())
        if idx is None:
            return
        if ev.button() == Qt.MouseButton.RightButton:
            # Right-click always clears; it must not pick a target, or there
            # would be no way back to editing the source key.
            self.keyRightClicked.emit(idx)
        elif self.picking_target:
            usage = KM_KEYS.get(idx, (None, None))[0]
            if usage is None or idx not in _MAPPABLE_KEYS:
                return
            self.usagePicked.emit(usage)
        else:
            self.keyClicked.emit(idx)


class ApplyWorker(QThread):
    done = pyqtSignal(bool, str)

    def __init__(self, payload, commit, opcode=OP_COLORS, attempts=25):
        super().__init__()
        self.payload = payload
        self.commit = commit
        self.opcode = opcode
        self.attempts = attempts

    def run(self):
        with _dev_lock:
            try:
                kb = Keyboard()
                try:
                    if self.opcode == OP_COLORS:
                        rep, exp = apply_config(kb, self.payload,
                                                commit=self.commit,
                                                attempts=self.attempts)
                    else:
                        rep, exp = apply_region(kb, self.opcode, self.payload,
                                                commit=self.commit,
                                                attempts=self.attempts)
                finally:
                    kb.close()
                ok, msg = checksum_summary(rep, exp, self.commit)
                self.done.emit(ok, msg)
            except Exception as e:  # noqa: BLE001
                self.done.emit(False, f"{type(e).__name__}: {e}")


class MacroApplyWorker(QThread):
    """Upload the macro region (0x0415). Its own worker because that region is
    not a keymap layer: the session shape differs (lead 0x0419, no 0x04F0
    commit) and the payload length follows the number of macros, so it cannot
    go through ApplyWorker without pretending to be a key layer.
    """

    done = pyqtSignal(object, object)

    def __init__(self, payload):
        super().__init__()
        self.payload = payload

    def run(self):
        with _dev_lock:
            try:
                kb = Keyboard()
                try:
                    rep, exp = apply_macro(kb, self.payload)
                finally:
                    kb.close()
                ok, msg = checksum_summary(rep, exp, False)
                self.done.emit(ok, msg)
            except Exception as e:  # noqa: BLE001
                self.done.emit(False, f"{type(e).__name__}: {e}")


class ReadStateWorker(QThread):
    """Best-effort live device-state read at startup.

    The C989's read-back replies are zero-filled (verified 2026-09-18), so this
    only replaces the cached state when real (non-zero) data arrives.
    """

    done = pyqtSignal(object)

    def run(self):
        with _dev_lock:
            try:
                kb = Keyboard()
                try:
                    cols = kb.read_colors(18)
                finally:
                    kb.close()
            except Exception:
                cols = b""
        self.done.emit(cols)


class FlashWorker(QThread):
    """Run flash.sh on a worker thread.

    `argv` is the full argument vector, e.g. [FLASH_SH, image_path]. It is
    passed to subprocess.run as-is -- do NOT wrap it in another list. Doing
    that made subprocess raise TypeError before flash.sh ever ran, so the GUI
    flash button silently reported failure and never flashed anything. That
    bug was present from ddb588a until now.

    The timeout is 300s rather than the old 120s: an ISP-mode flash reboots the
    keyboard twice (into the bootloader and back) and we have never timed one,
    so the old value was never a validated number and only ever a guess in the
    tight direction. Output is captured rather than inherited so the flashing
    banner stays out of the GUI's own terminal.
    """

    done = pyqtSignal(bool, str)

    def __init__(self, argv, timeout=300):
        super().__init__()
        self.argv = list(argv)
        self.timeout = timeout

    def run(self):
        try:
            if not self.argv:
                self.done.emit(False, "no command given")
                return
            if not os.path.isfile(self.argv[0]):
                self.done.emit(False, f"not found: {self.argv[0]}")
                return
            out = subprocess.run(self.argv, capture_output=True, text=True,
                                 timeout=self.timeout)
            msg = (out.stdout + out.stderr).strip()
            self.done.emit(out.returncode == 0, msg or f"exit {out.returncode}")
        except subprocess.TimeoutExpired:
            self.done.emit(False, f"timed out after {self.timeout}s")
        except Exception as e:  # noqa: BLE001
            self.done.emit(False, f"{type(e).__name__}: {e}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Cloudnine C989 — Lighting")
        self.state = LightState()
        self.state.on_perkey_change = self._palette_dirty
        self.current_color = QColor("#ff0000")
        self.paint_mode = False
        self._worker = None
        self._read_worker = None
        self._last_payload = b""
        self._color_sync = False
        # Live paint (OEM-style): edits and app-open re-push the config to the
        # board automatically via a debounced commit-upload, so colours change
        # the way the OEM app paints them - immediately, no Apply click.
        self._live_pending = False
        self._live_timer = QTimer(self)
        self._live_timer.setSingleShot(True)
        self._live_timer.setInterval(450)
        self._live_timer.timeout.connect(self._live_timer_fired)
        self._build_ui()
        # Custom colour slots: defaults if no saved copy, else restore.
        self.custom_colors = [QColor(hx) for hx in CUSTOM_DEFAULTS]
        cdata = read_cache(CUSTOM_COLORS_FILE, 24)
        if cdata:
            for j in range(8):
                self.custom_colors[j] = QColor(cdata[j * 3],
                                                cdata[j * 3 + 1],
                                                cdata[j * 3 + 2])
        self._refresh_custom()
        # Restore last-known state (the device can't read its own config back;
        # see the CACHE_DIR comment block). Best-effort live read follows.
        cols = read_cache(COLORS_CACHE, 1152)
        if cols is None:
            # Pre-strip28 cache: 144 entries sitting at 0x200. Rotate, or every
            # key comes back one entry out of place.
            cols = read_cache(COLORS_CACHE_OLD, 1152)
            if cols is not None:
                cols = bytes(_fit_key_region(bytearray(cols), OLD_KEY_REGION))
        if cols:
            self.state.buf = bytearray(cols)
        keymap = read_cache(KEYMAP_CACHE, 640)
        if keymap:
            # Into the layer dict, then re-point the live view at it. A plain
            # `self.remap_state.buf = ...` would rebind the view away from
            # _layer_bufs, and the canvas would then edit a buffer that Apply
            # never sends.
            self._layer_bufs[LAYER_TOP][:] = keymap
            self._show_layer_buf(LAYER_TOP)
        # The cache and any loaded .bin can come from an older build, so re-run
        # the brightness/speed snap on whatever just replaced the buffer --
        # otherwise a raw 0-255 byte blanks the board as soon as that effect is
        # selected.
        self.state.param_repaired += self.state._normalize_params()
        self._init_palettes()
        self._refresh_controls()
        if self.state.param_repaired:
            self.status.setText(
                f"repaired {self.state.param_repaired} brightness/speed value(s)"
                f" - the firmware reads those bytes as unbounded table indices, "
                f"and raw 0-255 values blank the keys")
        # The best-effort startup read (0x0412) is deliberately NOT done here.
        # Two independent reasons, and neither is a fix: (1) a status byte of
        # 0x00 in any reply is our own frame echoed back unpatched, never
        # device data (firmware FUN_000039cc only ever writes 0x01 or 0xFF), so
        # on_state_read could never do anything but discard the result; and
        # (2) the OEM app is write-only -- it never reads and never drains, and
        # our read+drain sequence made the 0x0418 lead answer 0x00 where the
        # OEM's answered 0x01. Removing it restores the lead ack to 01 on some
        # attempts, but the gate stayed shut through the 06:47 and the
        # post-power-cycle 06:53 validations, so read/drain is NOT the
        # discriminator. See docs/investigation.md.

    # -- per-key palettes -----------------------------------------------------
    # The device has exactly one per-key region, so it is always palette
    # `self.palette`'s working copy. The other one is parked in the GUI cache.
    def _region(self):
        return bytes(self.state.buf[KEY_REGION:ACTIVE_BLOCK])

    def _load_region(self, data):
        self.state.buf[KEY_REGION:ACTIVE_BLOCK] = data

    def _save_palettes(self):
        write_cache(PALETTES_FILE, self.palettes[0] + self.palettes[1])

    def _init_palettes(self):
        self.palette = 0
        live = self._region()
        self.palettes = [bytearray(live), bytearray(live)]
        saved = read_cache(PALETTES_FILE, REGION_LEN * 2)
        if saved is None:
            # Pre-strip28 file: two 576-byte 144-entry regions, each needing the
            # same 8-byte rotation as the main config buffer. They are bare
            # regions rather than whole configs, so each is dropped into a
            # throwaway 1152-byte buffer, rotated, and sliced back out.
            old = read_cache(PALETTES_FILE_OLD, OLD_REGION_LEN * 2)
            if old is not None:
                halves = []
                for i in range(2):
                    h = bytearray(PAYLOAD_LEN)
                    h[OLD_KEY_REGION:ACTIVE_BLOCK] = \
                        old[i * OLD_REGION_LEN:(i + 1) * OLD_REGION_LEN]
                    halves.append(bytes(
                        _fit_key_region(h, OLD_KEY_REGION)[KEY_REGION:ACTIVE_BLOCK]))
                assert len(halves[0]) == len(halves[1]) == REGION_LEN
                saved = halves[0] + halves[1]
        if saved:
            # Palette 0 stays the live region (it is what was last applied);
            # only the parked one is restored from disk.
            self.palettes[1] = bytearray(saved[REGION_LEN:])
        self._save_palettes()

    def _park_palette(self):
        """Copy the live region back into the palette being left behind."""
        self.palettes[self.palette] = bytearray(self._region())

    def _resync_palette(self):
        """Re-read the live region as the active palette (after a wholesale
        buffer replace: default restore, config load, or device read-back)."""
        self.palettes[self.palette] = bytearray(self._region())
        self._save_palettes()

    def _palette_dirty(self):
        """Per-key region was edited: fold it into the active palette and save.

        Wired to LightState.on_perkey_change, so this fires for paint, clear,
        clear-all and the zone helpers alike.
        """
        if not hasattr(self, "palettes"):
            return          # not initialised yet
        self.palettes[self.palette] = bytearray(self._region())
        self._save_palettes()

    def _start_state_read(self):
        self._read_worker = ReadStateWorker()
        self._read_worker.done.connect(self.on_state_read)
        self._read_worker.start()

    def on_state_read(self, cols):
        # Only trust the read-back when it looks like a real config (the active
        # slot carries its AA 55 terminator and some live data); anything else
        # is zero/garbage and must not clobber the restored cached state.
        if (cols and len(cols) == 1152
                and cols[0x440 + 14:0x440 + 16] == b"\xaa\x55"
                and any(cols[0x440:0x450])):
            self.state.buf = bytearray(cols)
            self._resync_palette()
            self._refresh_controls()
            self.status.setText(
                "loaded on-device lighting config (read-back responded)")
        elif cols and len(cols) == 1152:
            self.status.setText(
                "device read-back is zero-filled; showing last-applied/saved state")
        # The OEM app re-pushes its config on open; with live paint on we do
        # the same so the board matches whatever we are showing right away.
        self._schedule_live_paint()

    # -- UI ------------------------------------------------------------------
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)

        lighting = QWidget()
        self.tabs.addTab(lighting, "Lighting")
        lay = QVBoxLayout(lighting)

        # keyboard fills the top
        self.canvas = KeyboardCanvas(self.state)
        self.canvas.keyClicked.connect(self.on_key_clicked)
        self.canvas.keyRightClicked.connect(self.on_key_right)
        lay.addWidget(self.canvas, 3)

        # columns below the keyboard: Color, Zone, Effects, Parameters, Actions
        sec = QHBoxLayout()
        lay.addLayout(sec, 2)

        # colour
        col_box = QGroupBox("Color")
        cb = QVBoxLayout(col_box)
        self.swatch = QLabel()
        self.swatch.setMinimumHeight(26)
        self.swatch.setStyleSheet("background:#ff0000;border:1px solid #555")
        cb.addWidget(self.swatch)
        pick = QPushButton("Pick color…")
        pick.clicked.connect(self.pick_color)
        cb.addWidget(pick)
        pal = QGridLayout()
        for i, hx in enumerate(PALETTE):
            b = QPushButton()
            b.setFixedSize(26, 26)
            b.setStyleSheet(f"background:{hx};border:1px solid #333")
            b.clicked.connect(lambda _=False, c=hx: self.set_color(QColor(c)))
            pal.addWidget(b, i // 4, i % 4)
        cb.addLayout(pal)
        # Wrapped + shortened: this one label used to be 249px and was the sole
        # thing setting the Color column's width. The column is now bounded by
        # the C/M/Y spinbox row instead.
        _ccl = QLabel("Custom: click to use, right-click to save")
        _ccl.setWordWrap(True)
        cb.addWidget(_ccl)
        cust = QGridLayout()
        self.custom_btns = []
        for i in range(8):
            b = QPushButton()
            b.setFixedSize(26, 26)
            b.setStyleSheet("background:#808080;border:1px solid #333")
            b.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            b.customContextMenuRequested.connect(
                lambda _pos, idx=i: self.store_custom(idx))
            b.clicked.connect(
                lambda _=False, idx=i: self.set_color(self.custom_colors[idx]))
            cust.addWidget(b, i // 4, i % 4)
            self.custom_btns.append(b)
        cb.addLayout(cust)
        hxrow = QHBoxLayout()
        hxrow.addWidget(QLabel("Hex:"))
        self.hex_edit = QLineEdit()
        self.hex_edit.setPlaceholderText("#rrggbb")
        self.hex_edit.editingFinished.connect(self.on_hex_changed)
        hxrow.addWidget(self.hex_edit, 1)
        cb.addLayout(hxrow)
        # C/M/Y stacked rather than in one row: side by side they needed 204px
        # and were, after the label above was wrapped, the widest thing left in
        # this column. Stacked, the column is bounded by the hex field instead.
        cmygrid = QGridLayout()
        for r, (lbl, attr) in enumerate((("C:", "c_spin"), ("M:", "m_spin"),
                                         ("Y:", "y_spin"))):
            cmygrid.addWidget(QLabel(lbl), r, 0)
            sp = QSpinBox()
            sp.setRange(0, 255)
            setattr(self, attr, sp)
            sp.valueChanged.connect(self.on_cmy_changed)
            cmygrid.addWidget(sp, r, 1)
        cb.addLayout(cmygrid)
        cb.addStretch(1)
        sec.addWidget(col_box, 1)

        # zone column: the zones listed + per-key paint
        zn = QGroupBox("Zone — click to paint current color")
        zv = QVBoxLayout(zn)
        self.zone_list = QListWidget()
        self.zone_list.setFlow(QListView.Flow.TopToBottom)
        self.zone_list.setWrapping(True)
        self.zone_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.zone_list.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Same treatment as the effect list: top-to-bottom wrapping fills
        # columns, and a uniform grid cell makes the 6+5 split deterministic.
        # Zone labels are much longer than effect names, so this column is the
        # one that actually needs the extra width to afford the bigger font.
        _zf = QFont(self.zone_list.font())
        _zf.setPointSize(_ZONE_FONT_PT)
        self.zone_list.setFont(_zf)
        _zfm = QFontMetrics(_zf)
        _zcell = max(_zfm.horizontalAdvance(z) for z, i in ZONES) + 20
        _zrowh = _zfm.height() + 10
        self.zone_list.setGridSize(QSize(_zcell, _zrowh))
        self.zone_list.setFixedWidth(_zcell * _ZONE_COLS + 14)
        # +1 row of slack: the view reserves a row, so N*pitch only fits N-1
        # items per column and spills into a third column.
        self.zone_list.setFixedHeight(_zrowh * (_ZONE_ROWS + 1) + 6)
        for zname, idxs in ZONES:
            # Count lives in the tooltip, not the label: "(70 keys)" suffixes
            # cost ~5 font points of width, and 18pt (matching the effect list)
            # only fits the 1487px window without them. The count still shows in
            # the note line once you click a zone.
            it = QListWidgetItem(zname)
            it.setData(Qt.ItemDataRole.UserRole, zname)
            nstrip = sum(1 for i in idxs if i in _STRIP_BY_ENTRY)
            tip = self._zone_unit(idxs)
            if nstrip:
                # The strips are the one target here that depends on the
                # firmware, and they fail silently: on the rollback image
                # firmware.bin.m10 no entry routes into 119-147 at all, so
                # painting them looks identical to success and lights nothing.
                tip += ("\n\nNeeds firmware.strip28.bin. On "
                        "firmware.bin.m10 these entries reach no channel and "
                        "the strips stay dark no matter what is painted.")
            it.setToolTip(tip)
            self.zone_list.addItem(it)
        self.zone_list.itemClicked.connect(self.on_zone_apply)
        self.zone_list.itemDoubleClicked.connect(self.on_zone_clear)
        zv.addWidget(self.zone_list)
        self.paint_cb = QCheckBox("Paint keys on click (per-key)")
        self.paint_cb.stateChanged.connect(self.on_paint_toggle)
        zv.addWidget(self.paint_cb)
        zr = QHBoxLayout()
        zclear = QPushButton("Clear zone")
        zclear.clicked.connect(self.on_zone_clear)
        zr.addWidget(zclear)
        zall = QPushButton("Clear all keys")
        zall.clicked.connect(self.on_clear_keys)
        zr.addWidget(zall)
        zv.addLayout(zr)
        self.zone_note = QLabel()
        self.zone_note.setWordWrap(True)
        zv.addWidget(self.zone_note)
        sec.addWidget(zn, 2)

        # effects
        fx = QGroupBox("Effects — click one to make it active")
        fxh = QVBoxLayout(fx)
        self.effect_list = QListWidget()
        self.effect_list.setFlow(QListView.Flow.TopToBottom)
        self.effect_list.setWrapping(True)
        self.effect_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.effect_list.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # bare names, no hex prefix (kept as a tooltip). Top-to-bottom wrapping
        # fills columns instead of rows, and a uniform grid cell makes the split
        # deterministic: 8 items down each of 2 columns.
        _ef = QFont(self.effect_list.font())
        _ef.setPointSize(_EFFECT_FONT_PT)
        self.effect_list.setFont(_ef)
        _efm = QFontMetrics(_ef)
        _cell = max(_efm.horizontalAdvance(t) for _, t in EFFECT_LIST) + 20
        _rowh = _efm.height() + 10
        self.effect_list.setGridSize(QSize(_cell, _rowh))
        self.effect_list.setFixedWidth(_cell * 2 + 14)
        # 9 not 8: the view reserves a row of slack, so 8*pitch only fits 7
        # items per column and spills into a third column. 16 items / 8 = 2.
        self.effect_list.setFixedHeight(_rowh * 9 + 6)
        for eid, name in EFFECT_LIST:
            it = QListWidgetItem(name)
            it.setData(Qt.ItemDataRole.UserRole, eid)
            it.setToolTip(f"effect {_dev_id(eid):02x}")
            self.effect_list.addItem(it)
        self.effect_list.currentItemChanged.connect(self.on_effect_selected)
        fxh.addWidget(self.effect_list)
        # two columns of 8 fill this column's share, so it keeps stretch 3 and
        # the other four panels keep their width (see _EFFECT_FONT_PT)
        sec.addWidget(fx, 3)

        # parameters
        par = QGroupBox("Parameters")
        pg = QGridLayout(par)
        self.full = QCheckBox("Full spectrum")
        self.full.stateChanged.connect(self.on_full)
        pg.addWidget(self.full, 0, 0, 1, 2)
        pg.addWidget(QLabel("Brightness"), 1, 0)
        self.bri_label = QLabel("")
        pg.addWidget(self.bri_label, 1, 2)
        self.bri = QSlider(Qt.Orientation.Horizontal)
        # steps, not raw 0-255: see _BRI_INDEX for why the byte is a table index
        self.bri.setRange(0, len(_BRI_INDEX) - 1)
        self.bri.valueChanged.connect(self.on_bri)
        pg.addWidget(self.bri, 1, 1)
        pg.addWidget(QLabel("Speed"), 2, 0)
        self.spd_label = QLabel("")
        pg.addWidget(self.spd_label, 2, 2)
        self.spd = QSlider(Qt.Orientation.Horizontal)
        # steps, not raw 0-255: see _SPD_INDEX for why the byte is a table index
        self.spd.setRange(0, len(_SPD_INDEX) - 1)
        self.spd.valueChanged.connect(self.on_spd)
        pg.addWidget(self.spd, 2, 1)
        pg.addWidget(QLabel("Direction"), 3, 0)
        self.dir = QComboBox()
        self.dir.addItems(["Forward", "Reverse"])
        self.dir.currentIndexChanged.connect(self.on_dir)
        pg.addWidget(self.dir, 3, 1)
        sec.addWidget(par, 2)

        # actions
        act = QGroupBox("Actions")
        av = QVBoxLayout(act)
        self.live_cb = QCheckBox(
            "Paint the keyboard as you edit (live)")
        self.live_cb.setChecked(True)
        self.live_cb.setToolTip(
            "Push the config to the keyboard automatically: on app open and "
            "after every edit (debounced ~0.5 s), with commit so it survives "
            "a power cycle - exactly what the OEM app does. Untick to go back "
            "to explicit Apply buttons only.")
        self.live_cb.toggled.connect(self.on_live_toggled)
        av.addWidget(self.live_cb)
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setToolTip("Upload and commit the lighting config "
                                  "explicitly (survives power cycle)")
        self.apply_btn.clicked.connect(lambda: self.apply())
        av.addWidget(self.apply_btn)
        row = QHBoxLayout()
        save = QPushButton("Save…")
        save.clicked.connect(self.save_file)
        load = QPushButton("Load…")
        load.clicked.connect(self.load_file)
        row.addWidget(save)
        row.addWidget(load)
        av.addLayout(row)
        self.restore_btn = QPushButton("Restore OEM default")
        self.restore_btn.clicked.connect(self.restore_default)
        av.addWidget(self.restore_btn)
        av.addStretch(1)
        sec.addWidget(act, 1)

        self.tabs.addTab(self._build_wheel_tab(), "Smart wheel")
        self._remap_tab = self._build_remap_tab()
        self.tabs.addTab(self._remap_tab, "Key remap")
        self._macro_tab = self._build_macro_tab()
        self.tabs.addTab(self._macro_tab, "Macros")
        self.tabs.addTab(self._build_firmware_tab(), "Firmware")

        self.status = QLabel("ready")
        self.status.setWordWrap(True)
        self.status.setFrameShape(QFrame.Shape.StyledPanel)
        outer.addWidget(self.status)

    # -- smart wheel ---------------------------------------------------------
    # Flash page 0xA400 is 4 rows of [function, R, G, B] with an AA 55 marker at
    # 0x3E/0x3F, and it is the *only* thing that sets the wheel-ring colour.
    # Two different things hang off those 4 bytes:
    #
    #   * byte 0 is the smart-wheel FUNCTION bound to that detent. The wheel
    #     handler (0x3140) does b1b4 = (b1b4+1) & 3, reads the current detent's
    #     byte 0 (0xA400/4/8/C) and dispatches on it - cases 0,1,2,3 and also 6.
    #     The OEM app's per-place dropdown writes exactly this byte, and the
    #     values are the dropdown index: 0=Volume, 1=Task Switcher,
    #     2=Vertical scroll, 3=Brightness, 4=Horizontal scroll, 5=Zoom,
    #     6=Smart Wheel (="1033.lan" string ID - 255, verified live 2026-09-29:
    #     the app's 4 places are the 4 detent rows, and edits stream in the idle
    #     0x0425 data pages with no exit burst; the ring bank persisted the
    #     resulting bytes across an app restart). Cases 4/5 dispatch is not
    #     verified in the firmware, but the OEM app writes them, so we accept
    #     the full 0..6 range for parity.
    #   * bytes 1-3 are that detent's wheel-ring COLOUR. FUN_0000dea4 switches
    #     on b1b4 and copies the *selected* row's RGB into the R, G and B bytes
    #     of the four live ring channels 151-154 (table 0x6BA1 = 97 98 99 9A)
    #     - so the wheel ring shows one single colour at a time, not four
    #     different ones. (The old wording here said "all three LED regions";
    #     those three are the R/G/B planes of one 156-channel buffer, not
    #     separate regions.)
    #
    # FIRMWARE DEPENDENCY: FUN_0000dea4 is reached only through the rate-limited
    # call at 0xDFE2 in FUN_0000dfa8. renderfix/strip28 patched that path out
    # (0xDFD4 "b 0xDFEA"), so those images accept the 0x0425 write (checksum
    # MATCH) but can never repaint the ring. firmware.v1.0.bin
    # restores the OEM 8 bytes at 0xDFD4 on top of the known-good
    # strip28m10_unassigned image, which re-enables it.
    #
    # The warning that used to sit here ("DO NOT restore 0xDFD4: it lost the
    # side lights, the M10 fix and GUI colour writes") was RETRACTED
    # 2026-10-01. It rested on firmware.strip28wheel.bin, which restored
    # 0xDFD4 on top of stock's 0xd3 M10; the regressions tracked M10's
    # renderer-mode change, not the ring. The clean test - same 8 bytes, M10
    # inert (firmware.v1.0.bin) - was measured on device 2026-10-01:
    # the side lights survive, GUI key-colour paint/apply still take, and the
    # ring colour follows fn+click. The "three LED planes / every frame"
    # mechanism the warning cited was also wrong on three of four counts.
    # See docs/WHEEL_FUNCTION.md section 4a.
    #
    # Consequence: editing one row only changes what you see if the wheel
    # happens to be parked on that detent, and the host cannot read b1b4. So the
    # tab edits each row individually (function + colour), exactly like the OEM
    # app's per-place selector. OEM factory colours are #ffffff / #ff2020 /
    # #4c20ff / #20ff20.
    WHEEL_PRESETS = 4
    # Byte 0 of each detent row is the smart-wheel function. Values are the
    # OEM app's per-place dropdown index (= "1033.lan" ID - 255), decoded live
    # on 2026-09-29: 0=Volume 1=Task Switcher 2=Vertical scroll 3=Brightness
    # 4=Horizontal scroll 5=Zoom 6=Smart Wheel.
    WHEEL_FUNCTIONS = ("Volume", "Task Switcher", "Vertical scroll",
                       "Brightness", "Horizontal scroll", "Zoom", "Smart Wheel")
    _SWATCH = "background:%s;border:1px solid #333"

    def _build_wheel_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)
        desc = QLabel(
            "Smart-wheel settings - flash page 0xA400 (opcode 0x0425). The "
            "wheel has 4 positions; whichever one the wheel is parked on, that "
            "setting's colour lights the ring and its function runs when you "
            "turn it. Each row below is one setting - pick its colour and "
            "function, then Apply to write all four. "
            "The function list matches the OEM app's per-place selector "
            "(Volume, Task Switcher, Vertical scroll, Brightness, Horizontal "
            "scroll, Zoom, Smart Wheel). "
            "The ring ignores the brightness setting - it is written raw, "
            "without the brightness multiply the keys go through. "
            "This does not affect the side strips (LED1-LED19), which are "
            "separate hardware.")
        # Wrap rather than let a long sentence dictate the window width.
        desc.setWordWrap(True)
        desc.setMaximumWidth(560)
        lay.addWidget(desc)
        self.wheel_buf = bytearray(load_default("wheel_a.bin", 64))
        # Restore the last-applied wheel page (see WHEEL_CACHE). A
        # missing or wrong-sized cache falls back to the OEM default,
        # exactly like the colours/keymap caches.
        saved = read_cache(WHEEL_CACHE, 64)
        if saved:
            self.wheel_buf = bytearray(saved)

        lay.addSpacing(8)
        lay.addWidget(QLabel("What each wheel position does and shows:"))
        self.wheel_rows = []   # (function combo, colour button) per setting
        grid = QGridLayout()
        grid.addWidget(QLabel("Setting"), 0, 0)
        grid.addWidget(QLabel("Function"), 0, 1)
        grid.addWidget(QLabel("Ring colour"), 0, 2)
        for z in range(self.WHEEL_PRESETS):
            grid.addWidget(QLabel(str(z + 1)), z + 1, 0)
            fn = QComboBox()
            for f, name in enumerate(self.WHEEL_FUNCTIONS):
                fn.addItem(name, f)
            fn.setToolTip(
                "What this position does when the wheel is turned. Written to "
                "byte 0 of the row, exactly like the OEM app's per-place "
                "dropdown.")
            fn.currentIndexChanged.connect(
                lambda _i, row=z: self._on_wheel_fn(row))
            grid.addWidget(fn, z + 1, 1)
            sw = QPushButton()
            sw.setFixedSize(70, 24)
            sw.setToolTip(
                "Ring colour for this position. Click to change. "
                "Written to bytes 1-3 of the row.")
            sw.clicked.connect(lambda _c, row=z: self.pick_wheel_color(row))
            grid.addWidget(sw, z + 1, 2)
            self.wheel_rows.append((fn, sw))
        lay.addLayout(grid)
        self._refresh_wheel_ref()

        row = QHBoxLayout()
        wapply = QPushButton("Apply wheel settings")
        wapply.setToolTip("Uploads the whole 0xA400 page: all 4 functions and "
                          "colours as shown above.")
        wapply.clicked.connect(self.apply_wheel)
        row.addWidget(wapply)
        wreset = QPushButton("Reset to OEM default")
        wreset.clicked.connect(self.reset_wheel)
        row.addWidget(wreset)
        lay.addLayout(row)
        lay.addStretch(1)
        return tab

    def _refresh_wheel_ref(self):
        """Sync the per-setting dropdowns/swatches to the current buffer."""
        for z, (fn, sw) in enumerate(self.wheel_rows):
            fnv = self.wheel_buf[z * 4]
            with QSignalBlocker(fn):
                fn.setCurrentIndex(min(fnv, len(self.WHEEL_FUNCTIONS) - 1))
            r, g, b = self.wheel_buf[z * 4 + 1:z * 4 + 4]
            sw.setStyleSheet(self._SWATCH % f"#{r:02x}{g:02x}{b:02x}")

    def _on_wheel_fn(self, z):
        """The dropdown follows the combo: write byte 0 of that setting row."""
        self.wheel_buf[z * 4] = self.wheel_rows[z][0].currentData()

    def pick_wheel_color(self, z):
        r, g, b = self.wheel_buf[z * 4 + 1:z * 4 + 4]
        col = QColorDialog.getColor(QColor(r, g, b), self,
                                    f"Setting {z + 1} ring colour")
        if not col.isValid():
            return
        self.wheel_buf[z * 4 + 1:z * 4 + 4] = bytes(
            (col.red(), col.green(), col.blue()))
        self._refresh_wheel_ref()

    def reset_wheel(self):
        self.wheel_buf = bytearray(load_default("wheel_a.bin", 64))
        self._refresh_wheel_ref()
        self.status.setText("wheel colors/functions reset to OEM default "
                            "(not yet applied)")

    def apply_wheel(self):
        # Byte 0 is the wheel function, not a row number: it may be edited via
        # the function dropdown above ('_on_wheel_fn' writes it), and it must
        # stay a valid smart-wheel function (0..6), never a row index.
        # Re-stamp the AA 55 marker the page is expected to carry -
        # sonix_kbd.write_wheel does the same.
        self.wheel_buf[0x3E] = 0xAA
        self.wheel_buf[0x3F] = 0x55
        self._refresh_wheel_ref()
        payload = bytes(self.wheel_buf)
        self.rows = [(bytes(self.wheel_buf[z * 4 + 1:z * 4 + 4])
                      .hex().upper()) for z in range(self.WHEEL_PRESETS)]
        self.status.setText("uploading wheel colors/functions…")
        self._worker = ApplyWorker(payload, True, opcode=OP_WHEEL,
                                   attempts=WHEEL_ATTEMPTS)
        self._worker.done.connect(self.on_applied)
        self._worker.start()

    # -- macro library -------------------------------------------------------
    #
    # A list of named macros, in slot order: entry i is uploaded as slot i, so
    # the list IS the keyboard's macro table. Kept in the cache because the
    # keyboard will not give them back.

    def _load_macro_library(self):
        """[{'name': str, 'events': [(usage, down, delay), ...]}] from disk."""
        try:
            with open(macro_library_path()) as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return []
        out = []
        for entry in raw.get("macros", [])[:MACRO_SLOT_LIMIT]:
            events = [(int(e[0]), bool(e[1]), int(e[2]))
                      for e in entry.get("events", [])
                      if isinstance(e, (list, tuple)) and len(e) == 3]
            out.append({"name": str(entry.get("name", ""))[:64],
                        "events": events})
        return out

    def _save_macro_library(self):
        """Write the library out. A failure here is not fatal: the keyboard
        still holds the macros until the next upload."""
        data = {"version": 1, "macros": [
            {"name": m["name"],
             "events": [[u, int(d), dl] for u, d, dl in m["events"]]}
            for m in self.macros[:MACRO_SLOT_LIMIT]]}
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(macro_library_path(), "w") as fh:
                json.dump(data, fh, indent=1)
        except OSError:
            pass

    def _default_macro_name(self, events):
        if not events:
            return "empty macro"
        base = km_describe_macro(events)
        # Disambiguate repeats: two macros of "A S D" are still two macros.
        same = sum(1 for m in self.macros
                   if km_describe_macro(m["events"]) == base)
        return base if not same else f"{base} ({same + 1})"

    def _refresh_macro_list(self, select=None):
        """Rebuild the list widget from the library.

        Signals stay blocked across the whole rebuild, INCLUDING the row
        selection. Setting the row with signals live fires on_macro_selected,
        which binds a key and touches the canvas -- and this runs while the tab
        is still being built, so those widgets do not all exist yet. Qt aborted
        the process on the first library that had a macro in it (2026-10-03).
        """
        self.macro_list.blockSignals(True)
        self.macro_list.clear()
        for i, m in enumerate(self.macros):
            label = m["name"] or f"macro {i + 1}"
            n = len(m["events"]) // 2
            item = QListWidgetItem(f"{i}.  {label}   ({n} keys)")
            item.setData(Qt.ItemDataRole.UserRole, i)
            self.macro_list.addItem(item)
        if self.macros:
            row = 0 if select is None else max(0, min(select, len(self.macros) - 1))
            self.macro_list.setCurrentRow(row)
            self.macro_name_edit.setText(self.macros[row]["name"])
        else:
            self.macro_name_edit.setText("")
            self.macro_name_edit.setText("")
            self.macro_list.setEnabled(False)
            self.macro_bind_btn.setEnabled(False)
            self.macro_delete_btn.setEnabled(False)
        self.macro_list.blockSignals(False)
        self._sync_macro_slot_from_list()
        self.macro_apply_btn.setEnabled(bool(self.macros))
        if self.macros:
            self.macro_list.setEnabled(True)
            self.macro_bind_btn.setEnabled(True)
            self.macro_delete_btn.setEnabled(True)

    def _sync_macro_slot_from_list(self):
        """Follow the highlighted macro, so binding is a visible one-click act."""
        row = self.macro_list.currentRow()
        if row < 0:
            return
        self.remap_macro_slot.blockSignals(True)
        self.remap_macro_slot.setValue(min(row, self.remap_macro_slot.maximum()))
        self.remap_macro_slot.blockSignals(False)

    def on_macro_selected(self):
        row = self.macro_list.currentRow()
        if row < 0:
            return
        self.macro_name_edit.setText(self.macros[row]["name"])
        self._sync_macro_slot_from_list()
        self.on_remap_playmacro()
        self._populate_macro_editor(self.macros[row]["events"])

    def on_macro_renamed(self):
        row = self.macro_list.currentRow()
        if row < 0:
            return
        name = self.macro_name_edit.text().strip()[:64]
        if not name:
            return
        self.macros[row]["name"] = name
        self._refresh_macro_list(select=row)
        self._save_macro_library()
        self.status.setText(f"macro {row} renamed to {name}")

    def on_macro_delete(self):
        row = self.macro_list.currentRow()
        if row < 0:
            return
        name = self.macros[row]["name"] or f"macro {row + 1}"
        gone = self.macros.pop(row)
        self._refresh_macro_list(select=min(row, len(self.macros) - 1))
        self._save_macro_library()
        # Slots shift up, so every key bound above this one now points at the
        # wrong macro. Say so rather than leaving it silently mis-bound.
        shifted = [i for i in range(len(self.macros) + 1)
                   if self.remap_state.entry(i) is not None
                   and self.remap_state.entry(i)[:1] == b"\x06"
                   and split_playmacro(self.remap_state.entry(i)) is not None
                   and split_playmacro(self.remap_state.entry(i))[0] > row]
        self.status.setText(
            f"{name} deleted"
            + (f" - {len(shifted)} key(s) bound to a later slot now play a "
               f"different macro; check them" if shifted else ""))
        self._refresh_unsent()

    def on_macro_bind_selected(self):
        """Bind the selected key to the highlighted macro."""
        row = self.macro_list.currentRow()
        if row < 0:
            return
        self.remap_macro_slot.blockSignals(True)
        self.remap_macro_slot.setValue(min(row, self.remap_macro_slot.maximum()))
        self.remap_macro_slot.blockSignals(False)
        self.on_remap_playmacro()

    def _loopback_keys(self):
        """Keys on the active layer that play a macro, by name.

        Recording from this keyboard means its output is visible too, and a key
        bound to a macro sends that MACRO's keystrokes -- which then get
        recorded as though they were typed. Those keys cannot be identified from
        the /dev/input stream, so they are listed here instead of being hidden:
        the user can clear them, or avoid pressing them while recording.
        """
        out = []
        for idx in range(160):
            word = self.remap_state.entry(idx)
            if not word:
                continue
            if split_playmacro(word) is not None:
                out.append(KM_KEYS.get(idx, (None, f"idx {idx}"))[1])
        return out

    def _macro_target_row(self):
        """Where a new recording should go: over the highlighted macro."""
        row = self.macro_list.currentRow()
        return row if row >= 0 else len(self.macros)

    # -- macro recorder ------------------------------------------------------
    def _init_macro_recorder(self):
        """Wire the recorder and its live-update timer.

        The recorder itself is a plain thread with a lock (cloudnine_recorder),
        so the GUI never touches a Qt signal from it. Instead a timer polls
        it while recording, which also gives a natural place to notice a
        capture loop that died on its own.
        """
        self.macro_recorder = MacroRecorder()
        # The last recording, held until it is stored. The keyboard does not
        # hold it yet, and it cannot be read back, so the app is the only place
        # a recorded macro exists between Record and store.
        self._last_macro = []
        self._macro_dirty = False
        # The library, loaded from the cache. This is the app's copy of the
        # keyboard's macro table, in slot order.
        self.macros = self._load_macro_library()
        self._refresh_macro_list()
        self._macro_timer = QTimer(self)
        self._macro_timer.setInterval(150)
        self._macro_timer.timeout.connect(self._poll_recorder)
        # Folds into the recorder's poll: same job, same cadence.
        self._macro_focus_timer = QTimer(self)
        self._macro_focus_timer.setInterval(150)
        self._macro_focus_timer.timeout.connect(self._keep_focus_out_of_the_panel)
        # Nothing is recorded, so there is nothing to upload and no reason to
        # offer it. Both change only when a recording is stored.
        self.macro_apply_btn.setEnabled(False)
        if not recorder_available():
            self.macro_record_btn.setEnabled(False)
            self.macro_record_btn.setToolTip(
                "Recording needs python-evdev, which is not installed. "
                "Install it with: pip install evdev")
            self.remap_macro_live.setText(
                "Macro recording is unavailable: python-evdev is not "
                "installed. You can still bind a key to a macro slot by "
                "number.")
        else:
            self.remap_macro_live.setText(
                "Type the keys to record, then press Stop.")

    def _poll_recorder(self):
        """Show what has been recorded so far, while recording."""
        if not self.macro_recorder.running:
            # The loop can die by itself; say so rather than leaving an empty
            # "recording" on screen that looks like the user is not typing.
            self._macro_timer.stop()
            self._set_recording_ui(False)
            err = self.macro_recorder.error()
            events = rec_strip_stamps(self.macro_recorder.events())
            if err is not None:
                self._store_macro(events)
                self.remap_macro_live.setText(
                    f"Recording stopped: {type(err).__name__}: {err}")
            self.status.setText("macro recording stopped")
            return
        events = self.macro_recorder.events()
        text = self.macro_recorder.describe()
        dropped = self.macro_recorder.unmapped()
        line = f"recording: {text}"
        if dropped:
            names = ", ".join(sorted(dropped)[:4])
            extra = f" +{len(dropped) - 4}" if len(dropped) > 4 else ""
            line += f"   (not captured: {names}{extra})"
        if self.macro_recorder.truncated():
            line += f"   [stopped at {len(events)} events]"
        self.remap_macro_live.setText(line)

    def _set_recording_ui(self, live):
        """Swap Record/Stop, and get the keyboard out of the panel's widgets.

        While recording, every key you press is supposed to be captured, and an
        input widget with focus swallows it first. The slot spin box was the
        problem: it took focus, showed its number selected, and ate digits --
        so a macro could not contain a number, and every digit also silently
        re-bound the key to a different macro slot mid-recording.

        So during recording the spin box is disabled (visible greyed out, and
        unable to receive keys at all) and focus is pushed back out to the
        window, where digits mean nothing. Recording also always stores into
        slot 0, so there is nothing for the box to be doing at that moment.
        """
        self.macro_record_btn.setEnabled(not live and recorder_available())
        self.macro_stop_btn.setEnabled(live)
        self.remap_macro_slot.setEnabled(not live)
        if live:
            self.remap_macro_slot.clearFocus()
            self.remap_macro_slot.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            # Tab or a stray click could still land focus back in the panel, so
            # the panel is polled for the same reason the recorder is.
            self._macro_focus_timer.start()
        else:
            self._macro_focus_timer.stop()
            self.remap_macro_slot.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _keep_focus_out_of_the_panel(self):
        """Nothing in the macro panel may hold the keyboard while recording."""
        for widget in (self.remap_macro_slot, self._remap_macro_input):
            if widget.hasFocus():
                widget.clearFocus()

    def on_macro_fixed_toggled(self, on):
        """React to the fixed-delay box being turned off.

        The checkbox is on by default because it is the only delay shape
        confirmed to replay. Turning it off is allowed -- real timings are
        sometimes what you want -- but the label says so at once rather than
        leaving it to be discovered as a macro that replays three characters.

        The tooltip is set in full for both states rather than patched. An
        earlier version replaced the long one with a short sentence, which threw
        away the measurement that makes it persuasive.
        """
        if on:
            self.macro_fixed.setToolTip(
                "Gives every key the same short hold instead of your real "
                "timing.\n\n"
                "Checked, and it should stay checked. This is the OEM app's own "
                "fixed-delay recorder and the only delay shape confirmed to "
                "replay: on 2026-10-10 an 18-record macro recorded this way, "
                "with six Shift presses, played every record and printed its "
                "text exactly.")
        else:
            self.macro_fixed.setToolTip(
                "OFF: your real timings.\n\n"
                "Every macro recorded this way has failed to replay. They run "
                "for many seconds -- one measured 20 s, "
                "holding a single key for 5 s -- and the keyboard gives up "
                "part-way, leaving the last key held so the operating system "
                "auto-repeats it.\n\n"
                "Put it back on unless you have a reason.")
        if not on and not self.macro_recorder.running:
            self.status.setText(
                "fixed 10 ms delays are OFF - a macro recorded now will be "
                "hand-timed, and hand-timed macros do not replay")

    def on_record_macro_new(self):
        """Record into a new slot rather than over the highlighted macro."""
        self.macro_list.setCurrentRow(-1)
        self.on_record_macro()

    def on_record_macro(self):
        if self.macro_recorder.running:
            return
        if not self.macro_fixed.isChecked():
            answer = QMessageBox.question(
                self, "Record with real timings?",
                "The fixed 10 ms delays are off, so this macro will be "
                "recorded with the pauses you actually type at.\n\n"
                "That is the shape that does NOT replay. A hand-timed macro "
                "runs for many seconds -- one measured 20 s, holding a single "
                "key for 5 s -- and the keyboard gives up part-way and leaves "
                "the last key held, which auto-repeats into a burst of that "
                "character.\n\n"
                "Every macro recorded this way that has been tried failed. "
                "Turn the box back on and record again?")
            if answer != QMessageBox.StandardButton.Yes:
                self.macro_fixed.setChecked(True)
                self.status.setText(
                    "recording cancelled - fixed 10 ms delays turned back on")
                return
        self.macro_recorder.fixed_ms = 10 if self.macro_fixed.isChecked() else None
        try:
            self.macro_recorder.start()
        except RuntimeError as e:
            # start() raises rather than failing silently, because a recorder
            # that quietly captures nothing is worse than no recorder.
            QMessageBox.warning(self, "Cannot record", str(e))
            self.macro_record_btn.setEnabled(False)
            return
        self._set_recording_ui(True)
        self._macro_timer.start()
        self.status.setText("recording macro - press Stop when done")
        loopback = self._loopback_keys()
        if loopback:
            names = " ".join(loopback[:10])
            more = f" +{len(loopback) - 10} more" if len(loopback) > 10 else ""
            self.remap_macro_live.setText(
                f"recording. These keys already play a macro, so pressing one "
                f"records that MACRO instead of the key: {names}{more}")
        else:
            self.remap_macro_live.setText(
                "recording: nothing yet - every key you press now goes into "
                "the macro")
        # The slot box is disabled for the duration, so the note must not go on
        # talking about the number it happens to be showing.
        self.remap_macro_note.setText(
            "Recording. The macro will be stored in slot 0 and this key bound "
            "to it when you press Stop.")

    def on_stop_recording(self):
        if not self.macro_recorder.running:
            self._macro_timer.stop()
            self._set_recording_ui(False)
            return
        events = rec_strip_stamps(self.macro_recorder.stop())
        self._macro_timer.stop()
        self._set_recording_ui(False)
        if self.macro_recorder.error() is not None:
            self.remap_macro_live.setText(
                f"Recording failed: {self.macro_recorder.error()}")
            return
        if not events:
            self.remap_macro_live.setText(
                "Nothing was recorded. The recorder watches the system's "
                "keyboards through /dev/input, so it needs permission to read "
                "them - usually membership of the 'input' group.")
            return
        self._store_macro(events)
        self.remap_macro_note.setText(
            "Slot 0, played once. Press Apply macro to upload it to the "
            "keyboard, which replaces every macro already there.")
        n = len(events) // 2
        mode = " (fixed 10 ms delays)" if self.macro_fixed.isChecked() else ""
        self.remap_macro_live.setText(
            f"Recorded {n} key press{'es' if n != 1 else ''}: "
            f"{km_describe_macro(events)}{mode}")
        # Name what is wrong with the recording now, rather than letting it
        # surface later as nonsense coming out of the key.
        warns = recording_warnings(events)
        if warns:
            self.remap_macro_warn.setText("  •  ".join(warns))
            self.remap_macro_warn.setVisible(True)
        else:
            self.remap_macro_warn.setText("")
            self.remap_macro_warn.setVisible(False)
        dropped = self.macro_recorder.unmapped()
        if dropped:
            names = ", ".join(sorted(dropped)[:4])
            self.remap_macro_live.setText(
                f"Recorded {n} key press{'es' if n != 1 else ''}: "
                f"{km_describe_macro(events)}. Not captured: {names}"
                + (" (fixed 10 ms delays)" if self.macro_fixed.isChecked()
                   else ""))

    def show_macro_bytes(self):
        """Print the exact records of the last recording.

        The delay field's playback meaning is not settled (see the fixed-delay
        note above), so what we wrote needs to be readable, not assumed.
        """
        events = self._last_macro
        if not events:
            self.remap_macro_bytes.setText("nothing recorded yet")
            return
        blob = km_macro_events(events)
        lines = []
        for i, (usage, down, delay) in enumerate(events):
            rec = blob[i * 8:(i + 1) * 8]
            lines.append(f"{i:3d}  {rec.hex(' ')}  "
                         f"{km_usage_label(usage):6s} "
                         f"{'down' if down else 'up  '}  {delay} ms")
        self.remap_macro_bytes.setText(
            f"{len(events)} records, {len(blob)} bytes:\n"
            + "\n".join(lines))
        self._populate_macro_editor(events)

    def apply_macro_region(self):
        """Upload the whole macro library to the keyboard.

        Destructive by nature: the macro region is written whole, so whatever
        the keyboard held is replaced. Since read-back returns zeros on this
        firmware the app cannot read the old macros back, nor re-send them
        afterwards, so this asks first, and the cache file is what makes the
        library recoverable.
        """
        events = [m["events"] for m in self.macros if m["events"]]
        if not events:
            self.status.setText("record a macro first - nothing to upload")
            QMessageBox.information(
                self, "Nothing to upload",
                "Record a macro first, then apply it.")
            return
        if self._worker and self._worker.isRunning():
            self.status.setText("an upload is already running")
            return
        answer = QMessageBox.question(
            self, "Replace the keyboard's macros?",
            f"This uploads {len(events)} macro(s) and replaces every macro "
            f"the keyboard holds now, including any recorded with the OEM "
            f"application.\n\n"
            f"The keyboard cannot be read back, so the ones already there "
            f"cannot be recovered afterwards -- though this app's own macros "
            f"are kept in {macro_library_path()}.\n\n"
            f"Replace them?")
        if answer != QMessageBox.StandardButton.Yes:
            self.status.setText("macro upload cancelled")
            return
        self._macro_dirty = False
        payload = build_macro_region(events)
        self.status.setText("uploading macro…")
        self.macro_apply_btn.setEnabled(False)
        self._worker = MacroApplyWorker(payload)
        self._worker.done.connect(self.on_macro_applied)
        self._worker.start()

    def on_erase_all_macros(self):
        """Take every macro off the keyboard, leaving the library alone.

        The recovery path for a macro that replays and will not stop. That is a
        firmware state: nothing in the GUI can halt it, and the keyboard has to
        be unplugged. Removing the macros means no key has a block to point
        into, so pressing one does nothing at all.

        The saved library is deliberately not touched, so the macros are still
        there to re-upload once the cause is understood.
        """
        if self._worker and self._worker.isRunning():
            self.status.setText("an upload is already running")
            return
        answer = QMessageBox.question(
            self, "Erase all macros from the keyboard?",
            "This uploads an empty macro region, so no key on the keyboard can "
            "play a macro.\n\n"
            "Use this if a macro replays over and over and will not stop: the "
            "keyboard may still need unplugging afterwards, but nothing will "
            "replay once it is back in.\n\n"
            "Any key still bound to a macro slot will do nothing until you "
            "record and upload a macro again. The library saved by this app is "
            "not touched.\n\n"
            "Erase them?")
        if answer != QMessageBox.StandardButton.Yes:
            self.status.setText("macro erase cancelled")
            return
        self.status.setText("erasing macros…")
        self.macro_wipe_btn.setEnabled(False)
        self._worker = MacroApplyWorker(build_empty_macro_region())
        self._worker.done.connect(self.on_macro_applied)
        self._worker.start()

    def showEvent(self, event):
        """Hold the keyboard graphic at the size it has with no editor open.

        Measured once, on the first show, while the macro panel is still
        hidden -- which is the layout where the graphic gets all the room it
        wants. After that it is a minimum, so selecting a macro mode cannot
        resize the board.
        """
        super().showEvent(event)
        if self._remap_canvas_floor is None:
            self._remap_canvas_floor = self.remap_canvas.height()
        if self.remap_canvas.minimumHeight() < self._remap_canvas_floor:
            self.remap_canvas.setMinimumHeight(self._remap_canvas_floor)

    def _adopt_canvas_floor(self, size):
        """Whatever the graphic has, with no panel open, is what it wants.

        Guarded on the WINDOW size changing. An earlier version adopted on every
        resize, which fed back: raising the floor forced the window taller,
        which fired another resize, which raised it again -- the graphic crept up
        a couple of pixels every time the mode was changed (2026-10-10).
        """
        if size == self._remap_last_window_size:
            return
        self._remap_last_window_size = size
        if self._remap_macro_input.isVisible():
            return
        if self.remap_canvas.height() > self.remap_canvas.minimumHeight():
            # Not clamped: this is the graphic's natural height, so take it.
            self._remap_canvas_floor = self.remap_canvas.height()
            self.remap_canvas.setMinimumHeight(self._remap_canvas_floor)
            return
        # Clamped: the window just got SMALLER, and the graphic is only this
        # tall because the floor is still holding it there. Drop the floor, let
        # the layout settle, and measure what it really wants -- otherwise a
        # window could never be made shorter than it first was.
        self.remap_canvas.setMinimumHeight(210)
        QTimer.singleShot(0, self._measure_canvas_floor)

    def _measure_canvas_floor(self):
        """The graphic's natural height, with the floor temporarily off."""
        self._remap_canvas_floor = self.remap_canvas.height()
        self.remap_canvas.setMinimumHeight(self._remap_canvas_floor)

    def resizeEvent(self, event):
        """Keep the floor current as the window grows.

        Capturing it once at the first show was not enough: on a window that is
        later made taller, the graphic wants more room and the stale floor let
        the macro panel take some of it anyway. Whenever the window resizes with
        no macro panel open, whatever the graphic has is what it wants, so that
        becomes the new floor.
        """
        super().resizeEvent(event)
        # Deferred: a resize arrives BEFORE the layout has been recomputed, so
        # reading the graphic's height here gives last time's number and the
        # floor ends up stale. Let the event loop settle first.
        _size = (self.width(), self.height())
        QTimer.singleShot(
            0, lambda: self._adopt_canvas_floor(_size))

    def closeEvent(self, event):
        """Let a running upload finish before the window goes away.

        Every worker here is a QThread owned by this window. Closing the window
        mid-upload destroys them while run() is still executing, which Qt
        reports as "QThread: Destroyed while thread is still running" and then
        aborts the process. Waiting is cheap: the uploads take about a second,
        and the alternative is losing the write half-way.
        """
        for worker in (getattr(self, "_worker", None),
                       getattr(self, "_read_worker", None),
                       getattr(self, "_flash_worker", None)):
            if worker is not None and worker.isRunning():
                worker.wait(5000)
        # The recorder is a plain thread holding /dev/input descriptors, so it
        # is stopped explicitly rather than left to the daemon flag.
        rec = getattr(self, "macro_recorder", None)
        if rec is not None and rec.running:
            rec.stop()
        # A timer firing into a destroyed widget is a crash, and this one is
        # only running while recording.
        for timer in (getattr(self, "_macro_timer", None),
                       getattr(self, "_macro_focus_timer", None)):
            if timer is not None:
                timer.stop()
        event.accept()

    def on_macro_applied(self, ok, msg):
        self.macro_apply_btn.setEnabled(True)
        self.macro_wipe_btn.setEnabled(True)
        # Deliberately does NOT clear self._worker. The signal is emitted from
        # inside run(), so this slot can execute before run() has returned;
        # dropping the last reference here frees the QThread mid-run and Qt
        # aborts the process with "QThread: Destroyed while thread is still
        # running" (seen 2026-10-02 on the first macro upload). on_applied
        # keeps the reference for the same reason.
        if not ok:
            self.status.setText(f"macro upload failed: {msg}")
            return
        # Record what went out, so a later watcher run can tell whether the
        # device is holding this build of the encoding (see
        # macro_upload_stamp_path). The keyboard cannot be read back.
        try:
            with open(macro_upload_stamp_path(), "w") as fh:
                json.dump(macro_region_fingerprint(self.macros), fh, indent=1)
        except OSError:
            pass
        # The macro region and the keymap are SEPARATE uploads. Reporting
        # success here reads as "the key now plays this macro", which is only
        # true if the key layer has been applied too -- and it had not been, so
        # the key kept its factory mapping and just typed its own letter
        # (2026-10-03).
        pending = self._unsent_layers()
        if pending:
            names = " and ".join(LAYER_LABEL[lid] for lid in pending)
            self.status.setText(
                f"{msg} - but the KEY BINDING has not been sent: the "
                f"{names} layer still has unsent changes. Press Apply this "
                f"layer, or the key will not play the macro.")
        else:
            self.status.setText(msg)

    # There is no per-slot capacity, so there is nothing here to check per slot.
    # "Record into a later slot" was wrong advice from a wrong theory, and the
    # terminator pointer that replaced it was an unverified byte which changed
    # nothing on hardware (2026-10-07).
    #
    # What there IS to check is the property that does hold and that the
    # keyboard actually got wrong twice: our region must not be shorter than
    # the one already on the device, or the bytes past the end of our payload
    # keep the old `00 00 aa 55` and the region ends up with two end markers.
    # Every captured OEM region has exactly one.
    def _macro_region_problem(self, macros, region=None):
        """Report why a region would be unsafe to upload, or "" if it is fine.

        `region` is taken as given when supplied, so a caller (and the tests)
        can check a region that was not built here.
        """
        if region is None:
            region = build_macro_region([m["events"] for m in macros])
        n = sum(1 for i in range(len(region) - 3)
                if region[i:i + 4] == MACRO_SENTINEL)
        if n != 1:
            return (f"the region would hold {n} end markers; the keyboard "
                    f"needs exactly one, at its end")
        if len(region) < MACRO_REGION_MIN:
            return (f"the region is {len(region)} bytes, shorter than the "
                    f"{MACRO_REGION_MIN} the keyboard may already hold")
        return ""

    def _store_macro(self, events):
        """Put a recording into the library, named, and bind the key to it.

        Overwrites the highlighted macro, or appends if nothing is highlighted.
        Either way it is saved to the cache immediately: the keyboard cannot be
        read back, so this file is the only surviving copy, and an upload
        replaces every macro it holds.
        """
        self._last_macro = list(events)
        self._macro_dirty = True
        row = self._macro_target_row()
        if len(self.macros) >= MACRO_SLOT_LIMIT:
            # No slot to put it in. Overwrite the last one rather than grow a
            # library the action word has no byte for.
            row = MACRO_SLOT_LIMIT - 1
            self.status.setText(
                f"the library is full at {MACRO_SLOT_LIMIT} macros, so this "
                f"replaced slot {row}")
        entry = {"name": self._default_macro_name(events),
                 "events": list(events)}
        if row < len(self.macros):
            self.macros[row] = entry
        else:
            self.macros.append(entry)
        self._refresh_macro_list(select=row)
        self._save_macro_library()
        self.macro_name_edit.setText(self.macros[row]["name"])
        self.on_macro_bind_selected()
        problem = self._macro_region_problem(self.macros)
        if problem:
            self.status.setText(problem)
            self.remap_macro_warn.setText(problem)
            self.remap_macro_warn.setVisible(True)
            return
        self.status.setText(
            f"macro {row} saved as \"{self.macros[row]['name']}\" - press "
            f"Apply macro to upload it")


    def _populate_macro_editor(self, events):
        self.macro_events_table.setRowCount(len(events))
        for i, (usage, down, delay) in enumerate(events):
            from cloudnine_keymap import usage_label
            self.macro_events_table.setItem(i, 0, QTableWidgetItem(str(i)))
            self.macro_events_table.setItem(i, 1, QTableWidgetItem(usage_label(usage)))
            self.macro_events_table.setItem(i, 2, QTableWidgetItem("down" if down else "up"))
            self.macro_events_table.setItem(i, 3, QTableWidgetItem(str(delay)))

    def on_macro_add_event(self):
        row = self.macro_events_table.currentRow()
        if row < 0:
            row = self.macro_events_table.rowCount()
        self.macro_events_table.insertRow(row)

    def on_macro_del_event(self):
        row = self.macro_events_table.currentRow()
        if row >= 0:
            self.macro_events_table.removeRow(row)

    def on_macro_save_edits(self):
        row = self.macro_list.currentRow()
        if row < 0 or row >= len(self.macros):
            self.status.setText("no macro selected")
            return
        events = []
        from cloudnine_keymap import find_usage
        for i in range(self.macro_events_table.rowCount()):
            key_item = self.macro_events_table.item(i, 1)
            state_item = self.macro_events_table.item(i, 2)
            delay_item = self.macro_events_table.item(i, 3)
            if not key_item or not state_item or not delay_item:
                continue
            keytext = key_item.text().strip()
            usage = find_usage(keytext)
            if usage is None:
                try:
                    if keytext.startswith('0x'):
                        usage = int(keytext, 16)
                    else:
                        usage = int(keytext)
                except Exception:
                    continue
            state = state_item.text().strip().lower() == 'down'
            try:
                delay = int(delay_item.text().strip() or '0')
            except Exception:
                delay = 0
            events.append((usage, state, delay))
        self.macros[row]['events'] = events
        self._refresh_macro_list(select=row)
        self._save_macro_library()
        self._populate_macro_editor(events)
        try:
            self.show_macro_bytes()
        except Exception:
            pass
        self.status.setText(f"macro {row} edited")
    # -- key remap -----------------------------------------------------------
    def _build_macro_tab(self):
        """The macro editor, on a tab of its own between Key remap and Firmware.

        Macros used to be an editor inside the Key remap tab, which meant the
        tallest thing on that tab was sharing a stretch ratio with the keyboard
        graphic -- so selecting a macro mode resized the board. Capping it in
        two scroll areas made the graphic hold still but left everything
        scrolling.

        On its own tab it does not have to be capped at all. It is laid out in
        two columns, on the same line as before: what the key SENDS (record,
        the bytes, the event editor) on the left, and what is written to the
        keyboard (the library, the slot, the uploads) on the right.
        """
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(8, 8, 8, 8)

        intro = QLabel(
            "Macros are recorded here and written to the keyboard. The app "
            "cannot list what the keyboard already holds -- it answers "
            "read-back with zeros -- so the library below is the only record.")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        # Which layer a binding lands on. It matters here as much as on the Key
        # remap tab -- a macro bound here still writes the KEY's action word in
        # the keymap layer, not the macro region -- and it was not visible from
        # this tab at all.
        self._macro_layer_group, self._macro_layer_btns = \
            self._make_layer_selector()
        layerbar = QHBoxLayout()
        layerbar.addWidget(QLabel("Bindings go to:"))
        for btn in self._macro_layer_btns.values():
            layerbar.addWidget(btn)
        layerbar.addWidget(QLabel(
            "the same two buttons as the Key remap tab"), 1)
        outer.addLayout(layerbar)
        self._macro_layer_btns[LAYER_TOP].setChecked(True)

        # Bind or unbind a key from here, so the same two actions are not only
        # on the other tab. Separate buttons calling the same handlers: a widget
        # can only live on one tab.
        # Built here, attached at the bottom of the tab with the columns. The
        # row is where you finish a binding, so it belongs under the editor
        # rather than between the layer selector and the work.
        actions = QHBoxLayout()
        macro_clear = QPushButton("Clear this key's mapping")
        macro_clear.clicked.connect(self.on_remap_clear_key)
        macro_clear.setToolTip(
            "Put the selected key back to what the keyboard normally sends.")
        actions.addWidget(macro_clear)
        self.macro_clear_btn = macro_clear
        # Named layer_apply, not macro_apply: `macro_apply_btn` is already the
        # "Apply macro (upload to keyboard)" button in the other column, and
        # reusing the name silently handed it to this one.
        layer_apply = QPushButton("Apply this layer")
        layer_apply.clicked.connect(lambda: self.apply_remap())
        layer_apply.setToolTip(
            "Send this layer's key bindings to the keyboard. Recording a macro "
            "uploads the macro region; this sends the KEY's binding of it, "
            "which lives in the keymap layer.")
        actions.addWidget(layer_apply)
        self.macro_layer_apply_btn = layer_apply

        self.macro_layer_note = QLabel()
        self.macro_layer_note.setWordWrap(True)
        outer.addWidget(self.macro_layer_note)
        self._refresh_macro_layer_note()

        # A keyboard, so the key being assigned the macro can be picked here.
        # It shares `remap_state` with the one on the Key remap tab, so there
        # is one selected key whichever board you click.
        self.macro_canvas = KeyboardCanvas(self.remap_state, show_strips=False)
        self.macro_canvas.setToolTip(
            "Click the key this macro is for. The same key is selected on the "
            "Key remap tab -- a widget can only live on one tab, so the two "
            "boards are kept in step rather than being the same one.")
        self.macro_canvas.keyClicked.connect(self.on_macro_key_clicked)
        self.macro_canvas.keyRightClicked.connect(self.on_remap_key_right)
        self._boards.append(self.macro_canvas)
        outer.addWidget(QLabel("Key this macro is for:"))
        outer.addWidget(self.macro_canvas, 3)

        cols = QHBoxLayout()
        outer.addLayout(cols, 1)

        # The editor for "play macro": a slot NUMBER plus the macro recorder.
        #
        # The number rather than a list of the keyboard's own macros, because
        # this firmware answers read-back with zeros (verified 2026-10-02), so
        # the app cannot ask the device which macros exist. The recorder below
        # is how a macro gets here in the first place.
        hot_in = QWidget()
        ho = QVBoxLayout(hot_in)
        ho.setContentsMargins(0, 0, 0, 0)
        hot_in.setToolTip(
            "The macro library and what gets written to the keyboard. The app "
            "cannot list the macros the OEM application recorded: this keyboard "
            "answers read-back with zeros.")

        # The macro editor is split in two. It is the tallest thing on this tab,
        # and all of it in one column resized the keyboard graphic.
        #
        # This half -- record, show the bytes, edit the events -- is what the
        # key SENDS, so it goes in the input column beside the other per-key
        # editors. The rest, from the library down, is an operation on the
        # keyboard as a whole and belongs in the Actions column.
        macro_edit_in = QWidget()
        h2 = QVBoxLayout(macro_edit_in)
        h2.setContentsMargins(0, 0, 0, 0)
        macro_edit_in.setToolTip(
            "Record a macro, read back the bytes it produced, and edit the "
            "events in it.")

        # -- record ------------------------------------------------------
        # Fixed-delay mode reproduces the OEM app's own fixed-delay recording,
        # whose macros are confirmed to play on this keyboard. Real timings are
        # the default because they are what a person meant, but the delay field's
        # exact playback meaning is still being established -- a macro recorded
        # as "12Qawszx" replayed as "1111122222" (2026-10-03), which is a
        # firmware-side interpretation of the delay, not a malformed region.
        # Recording from every keyboard is unconditional, this one included.
        # The catch is stated when you press Record rather than hidden behind
        # an option: this keyboard's /dev/input node reports what the firmware
        # sends rather than what was pressed, so a key bound to a macro
        # contributes that MACRO's keystrokes to the recording. Those keys are
        # named by _loopback_keys() before recording starts.
        self.macro_fixed = QCheckBox("Fixed 10 ms delays (OEM's own format)")
        self.macro_fixed.setToolTip(
            "Gives every key the same short hold instead of your real timing.\n\n"
            "This is checked by default, and it should stay checked. It is the "
            "OEM app's own fixed-delay recorder, and it is the only delay shape "
            "confirmed to replay correctly: on 2026-10-10 an 18-record macro "
            "recorded this way, with six Shift presses, played every record and "
            "printed its text exactly.\n\n"
            "Real timings are why every earlier macro failed. They run for many "
            "seconds -- one measured 20 s, with a single key held for 5 s -- and "
            "the keyboard gives up part-way, leaving the last key held so the "
            "operating system auto-repeats it.")
        # On by default, because it is the only shape known to replay. Every
        # macro that has ever failed on this keyboard was hand-timed, and every
        # one that has played was recorded this way (2026-10-10).
        self.macro_fixed.setChecked(True)
        self.macro_fixed.toggled.connect(self.on_macro_fixed_toggled)
        rec_row = QHBoxLayout()
        self.macro_record_btn = QPushButton("Record macro")
        self.macro_record_btn.clicked.connect(self.on_record_macro)
        rec_row.addWidget(self.macro_record_btn)
        self.macro_stop_btn = QPushButton("Stop")
        self.macro_stop_btn.clicked.connect(self.on_stop_recording)
        self.macro_stop_btn.setEnabled(False)
        rec_row.addWidget(self.macro_stop_btn)
        h2.addLayout(rec_row)

        h2.addWidget(self.macro_fixed)

        self.remap_macro_live = QLabel()
        self.remap_macro_live.setWordWrap(True)
        h2.addWidget(self.remap_macro_live)

        # The exact bytes of the last recording. Playback of a real-timed macro
        # is not yet understood, so being able to read what was written is what
        # makes the difference between "it misbehaves" and a fix.
        self.remap_macro_warn = QLabel()
        self.remap_macro_warn.setWordWrap(True)
        self.remap_macro_warn.setStyleSheet("color: #b06000;")
        self.remap_macro_warn.setVisible(False)
        h2.addWidget(self.remap_macro_warn)

        self.macro_bytes_btn = QPushButton("Show recorded bytes")
        self.macro_bytes_btn.clicked.connect(self.show_macro_bytes)
        h2.addWidget(self.macro_bytes_btn)
        self.remap_macro_bytes = QLabel()
        self.remap_macro_bytes.setWordWrap(True)
        self.remap_macro_bytes.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        h2.addWidget(self.remap_macro_bytes)

        # Macro event editor
        h2.addWidget(QLabel("Edit macro events:"))
        self.macro_events_table = QTableWidget()
        self.macro_events_table.setColumnCount(4)
        self.macro_events_table.setHorizontalHeaderLabels(["#", "Key", "State", "Delay (ms)"])
        self.macro_events_table.setMinimumHeight(120)
        self.macro_events_table.setMaximumHeight(180)
        h2.addWidget(self.macro_events_table)

        macro_edit_row = QHBoxLayout()
        self.macro_add_btn = QPushButton("Add Event")
        self.macro_add_btn.clicked.connect(self.on_macro_add_event)
        macro_edit_row.addWidget(self.macro_add_btn)
        self.macro_del_btn = QPushButton("Delete Event")
        self.macro_del_btn.clicked.connect(self.on_macro_del_event)
        macro_edit_row.addWidget(self.macro_del_btn)
        self.macro_save_edit_btn = QPushButton("Save Edits")
        self.macro_save_edit_btn.clicked.connect(self.on_macro_save_edits)
        macro_edit_row.addWidget(self.macro_save_edit_btn)
        h2.addLayout(macro_edit_row)

        # -- the library ---------------------------------------------------
        # Named macros in slot order: entry i IS slot i, so what is listed here
        # is what the keyboard will hold after an upload.
        ho.addWidget(QLabel("Macros (this keyboard cannot be read back, so "
                            "this list is the record):"))
        self.macro_list = QListWidget()
        self.macro_list.setMaximumHeight(120)
        self.macro_list.currentRowChanged.connect(self.on_macro_selected)
        self.macro_list.setToolTip(
            "Highlight a macro to name it or to bind it to the selected key. "
            "Its position is its slot number on the keyboard.")
        ho.addWidget(self.macro_list)

        name_row = QHBoxLayout()
        self.macro_name_edit = QLineEdit()
        self.macro_name_edit.setPlaceholderText("name this macro…")
        self.macro_name_edit.returnPressed.connect(self.on_macro_renamed)
        name_row.addWidget(self.macro_name_edit, 1)
        rename = QPushButton("Rename")
        rename.clicked.connect(self.on_macro_renamed)
        name_row.addWidget(rename)
        self.macro_delete_btn = QPushButton("Delete")
        self.macro_delete_btn.clicked.connect(self.on_macro_delete)
        name_row.addWidget(self.macro_delete_btn)
        ho.addLayout(name_row)

        bind_row = QHBoxLayout()
        self.macro_bind_btn = QPushButton("Bind selected key to that macro")
        self.macro_bind_btn.clicked.connect(self.on_macro_bind_selected)
        bind_row.addWidget(self.macro_bind_btn)
        self.macro_new_btn = QPushButton("Record into a new macro")
        self.macro_new_btn.setToolTip(
            "Appends a new macro after the highlighted one. Recording over a "
            "highlighted macro replaces it instead.")
        self.macro_new_btn.clicked.connect(self.on_record_macro_new)
        bind_row.addWidget(self.macro_new_btn)
        ho.addLayout(bind_row)

        # -- the slot this key plays -------------------------------------
        hot_lbl = QLabel("This key plays macro (slot):")
        hot_lbl.setToolTip(
            "The slot number is what gets written to the keyboard. It is a "
            "number rather than a menu of the keyboard's macros because "
            "read-back returns zeros on this firmware, so the app cannot "
            "enumerate what the keyboard itself holds.")
        ho.addWidget(hot_lbl)
        slot_row = QHBoxLayout()
        self.remap_macro_slot = QSpinBox()
        self.remap_macro_slot.setRange(0, 15)
        self.remap_macro_slot.valueChanged.connect(
            lambda _v: self.on_remap_playmacro())
        slot_row.addWidget(self.remap_macro_slot)
        apply_macro = QPushButton("Use this slot instead")
        apply_macro.setToolTip(
            "Binds the selected key to whatever number is in this box, whether "
            "or not that macro is the one highlighted in the list.")
        apply_macro.clicked.connect(lambda: self.on_remap_playmacro())
        slot_row.addWidget(apply_macro, 1)
        ho.addLayout(slot_row)

        self.remap_macro_note = QLabel()
        self.remap_macro_note.setWordWrap(True)
        ho.addWidget(self.remap_macro_note)

        # -- upload -------------------------------------------------------
        # Recording only edits the slot list; the keyboard learns about a
        # macro when this region is uploaded. It is a separate button from
        # "Apply this layer" because the macro region is its own upload with
        # its own session shape (see cloudnine_app.apply_macro), and because
        # the upload REPLACES every macro the keyboard holds.
        up = QPushButton("Apply macro (upload to keyboard)")
        up.clicked.connect(self.apply_macro_region)
        up.setToolTip(
            "Upload the recorded macro to the keyboard. This replaces every "
            "macro the keyboard currently holds, including any recorded with "
            "the OEM application, which cannot be read back afterwards.")
        ho.addWidget(up)
        self.macro_apply_btn = up

        # An escape hatch, kept deliberately separate from the buttons above.
        # A macro that will not stop replaying is a firmware state, not
        # something the GUI can talk the keyboard out of, and the only way back
        # is to take its macros away: an empty region has no block for a key to
        # point into. This is the OEM app's own empty region byte for byte, so
        # it is the one macro payload known good on hardware without having been
        # watched mid-playback.
        wipe = QPushButton("Erase all macros from the keyboard")
        wipe.clicked.connect(self.on_erase_all_macros)
        wipe.setToolTip(
            "Upload an empty macro region, so no key can play a macro. Use "
            "this if a macro replays over and over and will not stop. The "
            "library saved in this app is not touched; only the keyboard is.")
        ho.addWidget(wipe)
        self.macro_wipe_btn = wipe

        left = QGroupBox("What the key sends")
        cl = QVBoxLayout(left)
        cl.addWidget(macro_edit_in)
        outer.addLayout(actions)
        cols.addWidget(left, 1)

        right = QGroupBox("What is written to the keyboard")
        cr = QVBoxLayout(right)
        cr.addWidget(hot_in)
        cols.addWidget(right, 1)

        self._remap_macro_edit_input = macro_edit_in
        self._remap_macro_input = hot_in
        # The recorder's widgets exist by now, so this can run.
        self._init_macro_recorder()
        return page

    def _build_remap_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)

        self.remap_state = KeymapState()
        # The firmware has two keymap banks: Top at 0x90C0 and Fn at 0xA440.
        # Each gets its own 640-byte buffer so switching layers in the UI does
        # not lose what was set on the other one, and remap_state.buf is a live
        # view of whichever one is active.
        self._layer_bufs = {LAYER_TOP: bytearray(km_default()),
                            LAYER_FN: bytearray(km_layer_default(LAYER_FN))}
        # What the keyboard is believed to hold, per layer. Anything that
        # differs is an unsent change. Seeded with the factory layer, which is
        # what the keyboard ships with, so an untouched layer reads as sent and
        # only real edits show up.
        self._layer_sent = {lid: bytes(b) for lid, b in
                            self._layer_bufs.items()}
        self.remap_state.layer = LAYER_TOP
        self.remap_canvas = KeyboardCanvas(self.remap_state, show_strips=False)
        # Every keyboard graphic, so a change to the state can repaint all of
        # them. The Macros tab adds a second one; see _build_macro_tab.
        self._boards = [self.remap_canvas]
        self._show_layer_buf(LAYER_TOP)
        self.remap_canvas.keyClicked.connect(self.on_remap_key_clicked)
        self.remap_canvas.keyRightClicked.connect(self.on_remap_key_right)
        self.remap_canvas.setSizePolicy(QSizePolicy.Policy.Expanding,
                                        QSizePolicy.Policy.Expanding)

        # -- which layer: two buttons along the top, like a tab strip --------
        # Same place the keyboard itself puts the Fn/Mod switch, so it reads as
        # "which layer am I on" rather than as another dropdown to hunt for.
        topbar = QHBoxLayout()
        self._layer_group, self._layer_btns = self._make_layer_selector()
        for btn in self._layer_btns.values():
            topbar.addWidget(btn)
        self._layer_btns[LAYER_TOP].setChecked(True)
        topbar.addWidget(QLabel(
            "Turquoise = this key does something other than its factory "
            "mapping. Click a key to edit; right-click to clear."),
            1)
        lay.addLayout(topbar)

        self.remap_layer_note = QLabel()
        self.remap_layer_note.setWordWrap(True)
        lay.addWidget(self.remap_layer_note)
        self._refresh_layer_note()

        lay.addWidget(self.remap_canvas, 3)

        # -- everything else in columns along the bottom, like Lighting ------
        cols = QHBoxLayout()
        lay.addLayout(cols, 2)

        # Layout preset. Narrow column, so the six presets stack as one button
        # each instead of a combo box: a combo hides the choice behind a popup
        # and the note underneath it needs the width more than the picker does.
        col_layout = QGroupBox("Layout — fill in every letter at once")
        cl = QVBoxLayout(col_layout)
        self._layout_group = QButtonGroup(self)
        self._layout_group.setExclusive(True)
        self._layout_btns = {}
        self.remap_layout_note = QLabel()
        self.remap_layout_note.setWordWrap(True)
        for lname in LAYOUT_NAMES:
            btn = QPushButton(lname)
            btn.setCheckable(True)
            btn.setToolTip(layout_label(lname))
            btn.clicked.connect(
                lambda _checked=False, l=lname: self.select_layout(l))
            self._layout_group.addButton(btn)
            self._layout_btns[lname] = btn
            cl.addWidget(btn)
        lay_btn = QPushButton("Apply to this layer")
        lay_btn.clicked.connect(self.on_remap_apply_layout)
        cl.addWidget(lay_btn)
        cl.addWidget(self.remap_layout_note)
        cl.addStretch(1)
        cols.addWidget(col_layout, 1)
        # Seed the selection directly rather than via select_layout(): that also
        # writes to the status bar, which does not exist until the tabs are
        # built. Nothing else on the page depends on which preset is highlighted.
        self.selected_layout = LAYOUT_NAMES[0]
        self._layout_btns[self.selected_layout].setChecked(True)
        self._layout_gap_note(self.selected_layout)

        # -- per-key action, split into two columns -------------------------
        # Left: which action, as buttons. Right: the input that action needs.
        # Splitting them means the buttons stay a fixed shape instead of being
        # squeezed by the target picker next to them, and the picker can be as
        # wide as the longest HID label.
        col_action = QGroupBox("Key — what to do")
        ca2 = QVBoxLayout(col_action)
        self.remap_sel = QLabel("no key selected")
        self.remap_sel.setWordWrap(True)
        ca2.addWidget(self.remap_sel)
        self._action_group = QButtonGroup(self)
        self._action_group.setExclusive(True)
        self._action_btns = {}
        for label, aid, ok in ACTION_TYPES:
            # Text and Launch are investigated but not offered: see
            # NOT_OFFERED in cloudnine_keymap. Their buttons are omitted rather
            # than shown disabled, because an "Apply" that cannot work is
            # worse than an absent feature. NO_BUTTON_ACTIONS is a different
            # case: that mode works, it is just reached from the Record macro
            # panel rather than from a button of its own.
            if aid in NOT_OFFERED or aid in NO_BUTTON_ACTIONS:
                continue
            btn = QPushButton(action_button_label(label))
            btn.setCheckable(True)
            btn.setToolTip(label if ok else
                           f"{label} — not available yet")
            btn.clicked.connect(
                lambda _checked=False, a=aid: self.select_action(a))
            self._action_group.addButton(btn)
            self._action_btns[aid] = btn
            ca2.addWidget(btn)
        ca2.addStretch(1)
        cols.addWidget(col_action, 1)

        col_input = QGroupBox("Key — what to send")
        ci = QVBoxLayout(col_input)
        self.remap_input_note = QLabel()
        self.remap_input_note.setWordWrap(True)
        ci.addWidget(self.remap_input_note)

        # The editor for "map to another key". The target is chosen by clicking
        # the key on the board above, which covers 107 of the catalog's 110
        # usages. There is deliberately no list of all 110 here: a combo box
        # would be a 110-row dropdown standing in for a choice the board
        # already shows in its real position. What is chosen is reported as
        # plain text, and the only leftovers get a 3-entry escape hatch below
        # because this keyboard physically lacks those keys.
        key_in = QWidget()
        ki = QVBoxLayout(key_in)
        ki.setContentsMargins(0, 0, 0, 0)
        self.remap_target_readout = QLabel()
        self.remap_target_readout.setWordWrap(True)
        self.remap_target_readout.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        ki.addWidget(self.remap_target_readout)
        self.remap_pick_hint = QLabel()
        self.remap_pick_hint.setWordWrap(True)
        ki.addWidget(self.remap_pick_hint)

        # Or name the key instead of hunting for it on the board. Typing "o"
        # or "f5" is faster than scanning 107 caps, and it is the same choice
        # made the other way -- both land on one HID usage.
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("or type a key name:"))
        self.remap_name_edit = QLineEdit()
        self.remap_name_edit.setPlaceholderText("e.g. o, F5, Enter, Space")
        self.remap_name_edit.setToolTip(
            "Type the key this one should send, by name or by the character "
            "it types. Press Enter.")
        self.remap_name_edit.editingFinished.connect(self.on_remap_name)
        name_row.addWidget(self.remap_name_edit, 1)
        ki.addLayout(name_row)
        self.remap_name_note = QLabel()
        self.remap_name_note.setWordWrap(True)
        ki.addWidget(self.remap_name_note)

        # Held modifiers. This is the same b0 as a plain remap -- B1 of the
        # action word carries the HID modifier bitmap, so Ctrl+V is 02 01 19 00
        # rather than a separate action type. The toggles sit with the target
        # choice because they modify the same word.
        mods_row = QHBoxLayout()
        mods_row.addWidget(QLabel("with:"))
        self.remap_mod_btns = {}
        self._mods_group = QButtonGroup(self)
        self._mods_group.setExclusive(False)   # any combination, not one of
        for bit, label in MODIFIER_CHOICES:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _c=False: self.on_remap_mods_changed())
            self._mods_group.addButton(btn)
            self.remap_mod_btns[bit] = btn
            mods_row.addWidget(btn)
        mods_row.addStretch(1)
        ki.addLayout(mods_row)
        ci.addWidget(key_in)
        self._remap_key_input = key_in
        self.remap_canvas.usagePicked.connect(self.on_remap_usage_picked)

        # The three catalog usages this board has no key for. Reachable, but
        # only because dropping them would be a silent regression.
        self.remap_offboard = QComboBox()
        for usage, label in HID_TARGETS:
            if usage not in _BOARD_USAGES:
                self.remap_offboard.addItem(
                    f"0x{usage:02x}  {label} (not on this board)", usage)
        self.remap_offboard.setToolTip(
            "These three are in the HID catalog but this keyboard has no key "
            "for them, so they cannot be picked from the board.")
        self.remap_offboard.currentIndexChanged.connect(
            lambda _i: self.on_remap_set())
        # Inside the key editor, not the column, so it disappears with the rest
        # of the target picker instead of lingering in the Text mode.
        ki.addWidget(self.remap_offboard)

        # The editor for "type a string": a text box, because a string is not
        # an enumerable set of values. Kept out of the key editor above so the
        # two modes never share a box and the meaning of what is typed in it
        # is never ambiguous.
        text_in = QWidget()
        ti = QVBoxLayout(text_in)
        ti.setContentsMargins(0, 0, 0, 0)
        ti.addWidget(QLabel("Text this key should type:"))
        self.remap_text_edit = QLineEdit()
        self.remap_text_edit.setPlaceholderText("type the string here")
        self.remap_text_edit.setToolTip(
            "What the key should type. The app cannot write this yet: it has "
            "not decoded how the keyboard stores a string, so it will not "
            "send bytes it cannot verify. See docs/ACTION_MODES.md.")
        self.remap_text_note = QLabel()
        self.remap_text_note.setWordWrap(True)
        ti.addWidget(self.remap_text_edit)
        ti.addWidget(self.remap_text_note)
        ci.addWidget(text_in)
        self._remap_text_input = text_in
        self.remap_text_edit.textChanged.connect(self.on_remap_text_changed)
        # "Text" has no button (NOT_OFFERED), so this editor is never shown.
        # It is kept built rather than removed so that restoring the button is
        # one line, matching the comment on NOT_OFFERED.
        text_in.setVisible(False)

        # The editor for "mouse function": eight buttons, in the vendor's own
        # order (1033.lan 331-338). A list of eight is a grid, not a dropdown.
        mouse_in = QWidget()
        mi = QVBoxLayout(mouse_in)
        mi.setContentsMargins(0, 0, 0, 0)
        mi.addWidget(QLabel("Mouse function:"))
        grid = QGridLayout()
        self._mouse_btns = []
        self._mouse_group = QButtonGroup(self)
        self._mouse_group.setExclusive(True)
        for n, (label, _sub, _b2) in enumerate(MOUSE_FUNCTIONS):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.clicked.connect(
                lambda _c=False, f=n: self.on_remap_mouse(f))
            self._mouse_group.addButton(btn)
            self._mouse_btns.append(btn)
            grid.addWidget(btn, n // 2, n % 2)
        mi.addLayout(grid)
        ci.addWidget(mouse_in)
        self._remap_mouse_input = mouse_in

        # The editor for "hot key": the same seven named shortcuts. Each shows
        # the combo it stands for, so "Show desktop" is legible without knowing
        # that it is Win+D.
        hot_in = QWidget()
        ho = QVBoxLayout(hot_in)
        ho.setContentsMargins(0, 0, 0, 0)
        ho.addWidget(QLabel("Shortcut:"))
        hgrid = QGridLayout()
        self._hot_btns = []
        self._hot_group = QButtonGroup(self)
        self._hot_group.setExclusive(True)
        for n, (label, mods, usage) in enumerate(HOTKEYS):
            btn = QPushButton(f"{label}  ({modifier_label(mods)}+"
                              f"{usage_label(usage)})")
            btn.setCheckable(True)
            btn.clicked.connect(
                lambda _c=False, f=n: self.on_remap_hotkey(f))
            self._hot_group.addButton(btn)
            self._hot_btns.append(btn)
            hgrid.addWidget(btn, n // 2, n % 2)
        ho.addLayout(hgrid)
        ci.addWidget(hot_in)
        self._remap_hot_input = hot_in

        # The editor for "media": seven buttons, same shape as the mouse grid.
        media_in = QWidget()
        md = QVBoxLayout(media_in)
        md.setContentsMargins(0, 0, 0, 0)
        md.addWidget(QLabel("Media key:"))
        mgrid = QGridLayout()
        self._media_btns = []
        self._media_group = QButtonGroup(self)
        self._media_group.setExclusive(True)
        for n, (label, _u) in enumerate(MEDIA_FUNCTIONS):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.clicked.connect(
                lambda _c=False, f=n: self.on_remap_media(f))
            self._media_group.addButton(btn)
            self._media_btns.append(btn)
            mgrid.addWidget(btn, n // 2, n % 2)
        md.addLayout(mgrid)
        ci.addWidget(media_in)
        self._remap_media_input = media_in


        # Clearing belongs to the selected key, not to either editor, so it
        # stays visible in every mode: a key remapped earlier can be cleared
        # even while a mode with no editor is selected.
        set_btn = QPushButton("Clear this key's mapping")
        self.remap_clear_btn = set_btn
        set_btn.setToolTip(
            "Put this key back to what the keyboard normally sends.")
        set_btn.clicked.connect(self.on_remap_clear_key)
        ci.addWidget(set_btn)
        ci.addStretch(1)
        cols.addWidget(col_input, 3)
        # Last, now the picker exists: "map to another key" is the only action
        # with a decoded encoding, so the input column opens on its editor.
        self.select_action(ACTION_KEY)
        # Both boards now exist, so the seeded preset can be drawn.
        self._show_layout_legends(self.selected_layout)

        # Apply / save / clear
        col_act = QGroupBox("Actions")
        ca = QVBoxLayout(col_act)
        # Says whether the key layer still has changes the keyboard has not
        # been sent. Without this the board showed a key turquoise and the note
        # said what it would do, and it was easy to believe the keyboard knew:
        # recording a macro and pressing "Apply macro" uploads the MACRO region
        # only, and the key's own binding lives in the keymap layer (0x0411),
        # which needs its own Apply. Pressing such a key then just types the key
        # (2026-10-03).
        self.remap_unsent = QLabel()
        self.remap_unsent.setWordWrap(True)
        ca.addWidget(self.remap_unsent)
        apply_btn = QPushButton("Apply this layer")
        apply_btn.clicked.connect(lambda: self.apply_remap())
        ca.addWidget(apply_btn)
        self.remap_apply_btn = apply_btn

        row = QHBoxLayout()
        save = QPushButton("Save keymap…")
        save.clicked.connect(self.save_remap)
        load = QPushButton("Load keymap…")
        load.clicked.connect(self.load_remap)
        row.addWidget(save)
        row.addWidget(load)
        ca.addLayout(row)

        row2 = QHBoxLayout()
        lsave = QPushButton("Save layout…")
        lsave.clicked.connect(self.save_named_layout)
        lload = QPushButton("Load layout…")
        lload.clicked.connect(self.load_named_layout)
        row2.addWidget(lsave)
        row2.addWidget(lload)
        ca.addLayout(row2)

        restore = QPushButton("Clear this layer")
        restore.clicked.connect(self.on_remap_clear_all)
        ca.addWidget(restore)
        clear_all = QPushButton("Clear ALL my key mappings…")
        clear_all.setToolTip(
            "Put every key on both layers back to the factory mapping and "
            "delete your saved layouts. Your lighting, per-key colours, "
            "palettes and the smart wheel are not touched.")
        clear_all.clicked.connect(self.on_clear_keymaps)
        ca.addWidget(clear_all)
        ca.addStretch(1)
        cols.addWidget(col_act, 1)
        # The graphic is not allowed to be squeezed by anything in this row.
        # The macro editor used to be here and was taller than the room there
        # is; it lives on its own tab now, but the floor is kept because it is
        # what makes the graphic's size independent of whatever editor is open.
        self._remap_canvas_floor = None
        self._remap_last_window_size = None

        return tab

    def _build_firmware_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)

        intro = QLabel(
            "Keyboard firmware. Two images: OEM (the untouched factory "
            "firmware) and v1.0 (ours - side lights, the Light key and the "
            "wheel ring). Flashing writes a full 64 KB image over USB, so the "
            "keyboard reboots into ISP mode and back. See "
            "docs/M10_FIX_FLASH.md.")
        intro.setWordWrap(True)
        intro.setMaximumWidth(720)
        lay.addWidget(intro)

        self.fw_status = QLabel()
        self.fw_status.setWordWrap(True)
        self.fw_status.setMaximumWidth(720)
        lay.addWidget(self.fw_status)

        self.fw_flash_v1_btn = QPushButton(
            "Flash firmware v1.0 (side lights + Light key + wheel ring)")
        self.fw_flash_v1_btn.clicked.connect(self.on_flash_v1)
        lay.addWidget(self.fw_flash_v1_btn)

        self.fw_flash_oem_btn = QPushButton(
            "Restore OEM firmware (original, unmodified)")
        self.fw_flash_oem_btn.clicked.connect(self.on_flash_oem)
        lay.addWidget(self.fw_flash_oem_btn)

        self._refresh_fw_status()
        lay.addStretch(1)
        return tab

    def _refresh_fw_status(self):
        code = firmware_light_code()
        if code is None:
            txt = ("Firmware v1.0: no image found next to the app, so the "
                   "Light (M10) key state cannot be read.")
        elif code == LIGHT_CODE_INERT:
            txt = ("Firmware v1.0: the Light (M10) key is inert - it emits "
                   "nothing and is not swallowed by the lighting cycle until "
                   "you assign it on the Key remap tab. The wheel ring follows "
                   "fn+click.")
        else:
            txt = "Firmware v1.0: unexpected Light-key byte 0x%02x." % code
        self.fw_status.setText(txt)
        # Both buttons are gated on their image being present, so the release
        # (OEM + v1.0) shows both live while a checkout missing an image greys
        # that one out rather than letting flash.sh fail on it.
        self.fw_flash_v1_btn.setEnabled(os.path.isfile(V1_FW))
        self.fw_flash_oem_btn.setEnabled(os.path.isfile(OEM_FW))

    def on_flash_v1(self):
        if not os.path.isfile(FLASH_SH):
            QMessageBox.warning(self, "Flash", f"flash.sh not found: {FLASH_SH}")
            return
        ret = QMessageBox.question(
            self, "Flash firmware v1.0",
            "This flashes firmware.v1.0.bin: our image with side-light "
            "(channel) support, the Light (M10) key set inert at 0x622f, and "
            "the wheel ring's recolour path restored so the ring follows "
            "fn+click.\n\n"
            "Measured on device 2026-10-01: the side lights survive, GUI "
            "key-colour paint/apply still take, and the ring colour follows "
            "fn+click. To go back to stock, use 'Restore OEM firmware' "
            "below.\n\n"
            "The keyboard will reboot into ISP mode and back. Continue?")
        if ret != QMessageBox.StandardButton.Yes:
            return
        self.fw_flash_v1_btn.setEnabled(False)
        self.status.setText("flashing firmware v1.0\u2026")
        self._flash_worker = FlashWorker([FLASH_SH, V1_FW])
        self._flash_worker.done.connect(self.on_flash_done)
        self._flash_worker.start()

    def on_flash_oem(self):
        if not os.path.isfile(FLASH_SH):
            QMessageBox.warning(self, "Flash", f"flash.sh not found: {FLASH_SH}")
            return
        ret = QMessageBox.question(
            self, "Restore OEM firmware",
            "This flashes firmware.oem.bin: the untouched factory firmware, "
            "exactly as the keyboard shipped.\n\n"
            "Everything our v1.0 firmware adds is reverted. Note the side-light "
            "channels stop working on the stock image (see README).\n\n"
            "The keyboard will reboot into ISP mode and back. Continue?")
        if ret != QMessageBox.StandardButton.Yes:
            return
        self.fw_flash_oem_btn.setEnabled(False)
        self.status.setText("restoring OEM firmware\u2026")
        self._flash_worker = FlashWorker([FLASH_SH, OEM_FW])
        self._flash_worker.done.connect(self.on_flash_done)
        self._flash_worker.start()

    def on_flash_done(self, ok, msg):
        self._refresh_fw_status()
        self.status.setText(("OK: " if ok else "FAILED: ") + msg)

    def _remap_note(self, idx, prefix="selected"):
        name = KM_KEYS.get(idx, (None, f"idx {idx}"))[1]
        if idx == LIGHT_IDX and not firmware_light_patch_ok():
            note = f"{name} (idx {idx}) — firmware intercept swallows M10; flash firmware v1.0 on the Firmware tab"
        elif idx in KM_UNMAPPABLE:
            note = f"{name} (idx {idx}) — not remappable"
        else:
            # One description for every decoded action type, so a key set to
            # a mouse function does not read as though it were a remap.
            tname = self.remap_state.describe(idx) or "unchanged"
            note = f"{name} (idx {idx}) -> {tname}"
        layer = LAYER_LABEL.get(self.remap_state.layer, "")
        self.remap_sel.setText(f"{prefix} ({layer}): {note}")
        # Nearly every key edit lands here, so this is the cheapest place to
        # notice the layer no longer matches the keyboard.
        self._refresh_unsent()
        # The modifier toggles and the mouse buttons both describe this key's
        # word, so they follow it.
        self._set_mod_btns(self.remap_state.target_mods(idx))
        self._sync_mouse_btns(idx)
        self._sync_media_btns(idx)
        self._sync_hot_btns(idx)
        # Keep the slot box on whichever macro this key already plays, so
        # changing it is a deliberate act rather than a re-bind on select.
        self._sync_macro_slot(idx)
        # Keep the target readout showing what THIS key currently sends.
        if self.selected_action == ACTION_KEY:
            self._set_target(self.remap_state.target_usage(idx))
            self._arm_target_picker(True)

    def _repaint_boards(self):
        """Redraw every keyboard graphic.

        There are two of them -- one on the Key remap tab and one on the Macros
        tab -- and they share `remap_state`. A widget can only have one parent,
        so they cannot be the same widget, and anything that changes the state
        has to repaint both or the other tab shows a stale board.
        """
        for b in getattr(self, "_boards", ()):
            b.update()

    def _select_key_everywhere(self, idx):
        """The same key is selected on both boards."""
        for b in getattr(self, "_boards", ()):
            b.selected = idx

    def on_remap_key_clicked(self, idx):
        self.remap_state.selected = idx
        self._select_key_everywhere(idx)
        self._repaint_boards()
        self._remap_note(idx)

    def on_macro_key_clicked(self, idx):
        """Pick the key a macro is being assigned to, from the Macros tab.

        The same selection as clicking it on the Key remap tab: the slot box and
        "Bind selected key to that macro" act on `remap_state.selected`, so
        there is one selected key, not one per board.

        No guard on which keys are allowed, deliberately. An earlier version
        refused anything outside `_MAPPABLE_KEYS`, which is the set of keys that
        can be given *another key's* HID usage -- and it excludes the ten macro
        keys, because the usages they send are reserved and so never appear as
        targets. That is the wrong question: it refused M1, M2, M3, M4, M7 and
        M10 on the tab whose whole purpose is binding macros to them (2026-10-11).

        Whether a key can actually take a mapping is reported by _remap_note,
        the same as everywhere else, rather than guessed at here.
        """
        self.remap_state.selected = idx
        self._select_key_everywhere(idx)
        self._repaint_boards()
        self._remap_note(idx)
        self._sync_macro_slot(idx)
        self.remap_macro_note.setText(
            "%s is selected. Slot %d is what this key will play."
            % (self._sel_name(), self.remap_macro_slot.value()))

    def on_remap_key_right(self, idx):
        self.remap_state.clear(idx)
        self.remap_canvas.target_usage = None
        self._repaint_boards()
        self._remap_note(idx)
        self.status.setText(f"cleared remap on key {idx}")

    def on_remap_set(self):
        """Apply the chosen target to the selected key.

        Reached from the off-board escape hatch only: the board click applies
        itself through on_remap_usage_picked. The chosen target lives on the
        canvas, so there is no separate pending value that could disagree with
        what is drawn.
        """
        idx = self.remap_state.selected
        if idx is None:
            return
        if not ACTION_AVAILABLE[self.selected_action]:
            self.status.setText(
                "that action is not available yet - nothing was changed")
            return
        usage = self.remap_offboard.currentData()
        if usage is None:
            return                      # nothing chosen; the board is the picker
        if self.remap_state.set_remap(idx, usage, self._selected_mods()):
            mods = self._selected_mods()
            note = (f"{KM_KEYS.get(idx, (None, 'key'))[1]} -> "
                    f"{modifier_label(mods) + '+' if mods else ''}"
                    f"{usage_label(usage)}")
        else:
            note = f"key {idx} not remappable"
        self._set_target(usage)
        self._repaint_boards()
        self._remap_note(idx)
        self.status.setText(note)

    def _disable_selected_key(self):
        """Make the selected key send nothing."""
        idx = self.remap_state.selected
        if idx is None:
            self.status.setText("pick a key first, then Disable key")
            return
        if self.remap_state.set_word(idx, DISABLE_WORD):
            self._repaint_boards()
            self._remap_note(idx)
            self.status.setText(
                f"{self._sel_name()} disabled - it will send nothing")

    def on_remap_clear_key(self):
        """Clear just the selected key's mapping (right-click also does this)."""
        idx = self.remap_state.selected
        if idx is None:
            self.status.setText("no key selected")
            return
        self.remap_state.clear(idx)
        self._repaint_boards()
        self._set_target(None)
        self.status.setText(
            f"{self._sel_name()} cleared - press Apply to send the change")

    def _show_layer_buf(self, lid):
        """Point the canvas view at `lid`'s buffer and repaint."""
        self.remap_state.layer = lid
        self.remap_state.buf = self._layer_bufs[lid]
        self._repaint_boards()

    def _make_layer_selector(self):
        """A Top/Fn button pair, and the group holding it.

        Built twice, once for the Key remap tab and once for the Macros tab: a
        widget can only live on one tab, so the two cannot be shared. Both are
        driven from `select_layer`, which checks every set, so they cannot
        disagree about which layer is being edited.
        """
        group = QButtonGroup(self)
        group.setExclusive(True)
        btns = {}
        for lid in LAYERS:
            btn = QPushButton(LAYER_LABEL[lid])
            btn.setCheckable(True)
            btn.setToolTip(
                "The keys that work while Fn is held."
                if lid == LAYER_FN else
                "The keys that work when Fn is not held.")
            btn.clicked.connect(
                lambda _checked=False, l=lid: self.select_layer(l))
            group.addButton(btn)
            btns[lid] = btn
        return group, btns

    def select_layer(self, lid):
        """Switch which firmware layer the canvas edits (the top buttons)."""
        if lid not in LAYERS:
            lid = LAYER_TOP
        # setChecked() does not re-emit clicked, so this cannot recurse.
        for btns in self._all_layer_btns():
            btns[lid].setChecked(True)
        self._show_layer_buf(lid)
        if self.remap_state.selected is not None:
            self._remap_note(self.remap_state.selected)
        if lid == LAYER_FN:
            self.status.setText("Fn layer: these mappings apply while Fn is held")
        else:
            self.status.setText("Top layer: these mappings apply normally")
        self._refresh_layer_note()

    def _refresh_macro_layer_note(self):
        """Say which layer a binding made here lands on, and whether it is sent.

        A macro upload sends the macro REGION. The key's own action word lives in
        the keymap layer, so a binding still needs "Apply this layer" on the Key
        remap tab -- and from this tab there was no way to see which layer that
        was or whether it had been sent.
        """
        lid = self.remap_state.layer
        pending = self._unsent_layers()
        here = f" This layer has unsent changes." if lid in pending else ""
        self.macro_layer_note.setText(
            f"A key bound here plays a macro slot; the binding itself is a "
            f"change to the {LAYER_LABEL[lid]} key layer, applied by "
            f"'Apply this layer' on the Key remap tab.{here}")

    def _all_apply_btns(self):
        """Every "Apply this layer" button, so all of them show the same state.

        There is one on the Key remap tab and one on the Macros tab. They must
        agree about the unsent marker, or the tab you are not looking at claims
        the layer is sent when it is not.
        """
        return [b for b in (getattr(self, "remap_apply_btn", None),
                            getattr(self, "macro_layer_apply_btn", None)) if b]

    def _all_layer_btns(self):
        """Every layer-button set that exists, so they are all kept in step."""
        out = []
        for name in ("_layer_btns", "_macro_layer_btns"):
            btns = getattr(self, name, None)
            if btns:
                out.append(btns)
        return out

    def _unsent_layers(self):
        """[layer id] whose buffer differs from what was last uploaded."""
        return [lid for lid in LAYERS
                if bytes(self._layer_bufs[lid]) != self._layer_sent.get(lid)]

    def _refresh_unsent(self):
        """Show, and mark on the button, that key changes are not yet on the device."""
        pending = self._unsent_layers()
        if not pending:
            self.remap_unsent.setText(
                "This key layer matches what was last uploaded.")
            for _b in self._all_apply_btns():
                _b.setText("Apply this layer")
            if getattr(self, "macro_layer_note", None) is not None:
                self._refresh_macro_layer_note()
            return pending
        names = " and ".join(LAYER_LABEL[lid] for lid in pending)
        one = "change" if len(pending) == 1 else "changes"
        self.remap_unsent.setText(
            f"Not sent to the keyboard yet: {one} on the {names} layer. "
            f"Press Apply this layer -- uploading a macro does not send the "
            f"key's binding.")
        for _b in self._all_apply_btns():
            _b.setText("Apply this layer  *")
        # The Macros tab has its own copy of this state on screen.
        if getattr(self, "macro_layer_note", None) is not None:
            self._refresh_macro_layer_note()
        return pending

    def _layer_now_sent(self, lid):
        """Record a layer as matching the keyboard, after an accepted upload."""
        self._layer_sent[lid] = bytes(self._layer_bufs[lid])
        self._refresh_unsent()

    def _refresh_layer_note(self):
        lid = self.remap_state.layer
        if lid == LAYER_FN:
            self.remap_layer_note.setText(
                "The Fn layer is written separately (0x0427). It is written for "
                "real, exactly as the OEM app does it; what has not been "
                "checked is whether a commit made from here survives a power "
                "cycle.")
        else:
            self.remap_layer_note.setText(
                "The Top layer is what the keyboard uses when Fn is not held.")

    def _layout_gap_note(self, lname):
        """Warn about characters a layout needs that the keyboard cannot type."""
        gap = layout_unrepresentable(lname)
        if not gap:
            self.remap_layout_note.setText("")
            return ""
        return ("\n\nThis keyboard cannot type " + " ".join(gap) +
                ", because the HID keyboard page has no key code for them. "
                "Those keys are left alone. To get them, set your operating "
                "system to the matching language layout - the app only moves "
                "the letters.")

    def select_layout(self, lname):
        """Pick the layout preset the Apply button will use."""
        if lname not in LAYOUT_NAMES:
            return
        self.selected_layout = lname
        # setChecked() does not re-emit clicked, so this cannot recurse.
        self._layout_btns[lname].setChecked(True)
        self._show_layout_legends(lname)
        self.on_remap_layout()

    def _show_layout_legends(self, lname):
        """Redraw both keyboards with `lname`'s caps in place.

        The remap board and the lighting board are the same physical keys, so
        a preview that only moved one of them would contradict the other. Both
        get the same {key_index: character} map, and both fall back to their
        US legends when the preset moves nothing (QWERTY, Mac).
        """
        legends = layout_legends(lname) if lname else {}
        for canvas in (self.remap_canvas, self.canvas):
            canvas.legends = legends
            canvas.update()

    def on_remap_layout(self):
        """Show what the highlighted layout preset would change."""
        lname = self.selected_layout
        if not lname:
            return
        # Called for its side effect: it fills in the gap warning label.
        self._layout_gap_note(lname)
        changes = layout_remaps(lname)
        if not changes:
            self.status.setText(
                f"{lname}: matches this layer, nothing to change")
            return
        sample = ", ".join(
            f"{KM_KEYS.get(i, (None, f'idx {i}'))[1]} to {usage_label(u)}"
            for i, u in list(changes.items())[:3])
        more = "" if len(changes) <= 3 else f" (+{len(changes) - 3} more)"
        self.status.setText(
            f"{lname} would change {len(changes)} keys: {sample}{more}")

    def on_remap_apply_layout(self):
        lname = self.selected_layout
        if not lname:
            return
        gap = self._layout_gap_note(lname)
        lid = self.remap_state.layer
        self.remap_state.buf[:] = layout_payload(lname)
        self._repaint_boards()
        if self.remap_state.selected is not None:
            self._remap_note(self.remap_state.selected)
        self.status.setText(
            f"applied {lname} to the {LAYER_LABEL[lid].lower()}"
            f" ({len(layout_remaps(lname))} keys) - press Apply to send it{gap}")

    def select_action(self, aid):
        """Pick what the selected key should do, and show what it needs.

        The input column follows the action: "map to another key" needs a
        target HID usage, so the picker appears, and everything else needs
        something we have not decoded, so the note appears instead. Without
        this the button row would silently do nothing when the action has no
        editor.
        """
        if aid not in self._action_btns:
            return
        self.selected_action = aid
        self._action_btns[aid].setChecked(True)
        # "Disable key" has nothing to choose, so there is no editor for it:
        # picking the action IS the decision, and it applies at once the way a
        # board click does. Anything else would mean a confirm button whose
        # only content is the word Disable.
        if aid == ACTION_DISABLE:
            self._disable_selected_key()
        # Every mode hides every editor first, then shows its own. Written as
        # "hide all" rather than as a chain of branches so that adding a mode
        # cannot leave another mode's editor on screen.
        # The macro editor is not in this list: it lives on its own tab, so
        # there is nothing here to hide or show for it.
        for box in (self._remap_key_input, self._remap_text_input,
                    self._remap_mouse_input, self._remap_media_input,
                    self._remap_hot_input):
            box.setVisible(False)
        # Whether the action is implemented decides the note, not which branch
        # we happen to fall into. Mouse and media are both implemented but share
        # no editor with the key picker, so they need a branch of their own.
        self.remap_input_note.setText(
            "" if ACTION_AVAILABLE.get(aid) else
            "This action is not available yet. The keyboard supports it, "
            "but we have not decoded how it is stored, so the app will not "
            "write bytes it cannot verify.")
        if aid in (ACTION_KEY, ACTION_COMBO):
            self._remap_key_input.setVisible(True)
        elif aid == ACTION_TEXT:
            self._remap_text_input.setVisible(True)
            self.on_remap_text_changed()
        elif aid == ACTION_MOUSE:
            self._remap_mouse_input.setVisible(True)
        elif aid == ACTION_MEDIA:
            self._remap_media_input.setVisible(True)
        elif aid == ACTION_HOTKEY:
            self._remap_hot_input.setVisible(True)
        elif aid in (ACTION_PLAYMACRO, ACTION_RECORD):
            # The editor is on the Macros tab now, so go there rather than
            # showing an empty column here. Recording and playing are the same
            # operation: the recorder captures a macro, and the slot box is the
            # number written to the keyboard.
            self._arm_target_picker(False)
            self.remap_macro_note.setText(
                "Slot %d, played once. Recording captures a macro; the slot is "
                "the number written to the keyboard. The editor is on the "
                "Macros tab."
                % self.remap_macro_slot.value())
            self._show_macro_tab(aid)
            return
        elif aid == ACTION_DISABLE:
            self.remap_input_note.setText(
                "This key will send nothing when pressed. Clearing it later "
                "brings it back.")
        # Coming the other way: having been sent to the Macros tab by a macro
        # action, choosing anything else brings the Key remap tab back, so the
        # tab you are on keeps matching the mode you are editing.
        if self._on_macro_tab():
            self._show_key_remap_tab()
        # "Combo key" is the same action word with a modifier bitmap, so it
        # arms the same board picker. Only the working actions arm it, so the
        # board never swallows a click that was meant to select a key.
        self._arm_target_picker(aid in (ACTION_KEY, ACTION_COMBO))

    def _on_macro_tab(self):
        """True if the Macros tab is the one being shown."""
        page = getattr(self, "_macro_tab", None)
        if page is None:                 # not built yet; _build_remap_tab runs first
            return False
        return self.tabs.currentIndex() == self.tabs.indexOf(page)

    def _show_key_remap_tab(self):
        _i = self.tabs.indexOf(self._remap_tab)
        if _i >= 0:
            self.tabs.setCurrentIndex(_i)

    def _show_macro_tab(self, aid=None):
        """Switch to the Macros tab, if it is not already showing.

        Called when a macro action is picked on the Key remap tab, so the
        editor is never somewhere the user is not looking.
        """
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == "Macros":
                if self.tabs.currentIndex() != i:
                    self.tabs.setCurrentIndex(i)
                break

    def _arm_target_picker(self, on):
        """Turn the board into a target picker, or back into a key selector.

        While armed, a click on the board chooses the key the selected one
        should send instead of changing which key is selected. Right-click
        still clears, so there is always a way back.
        """
        self.remap_canvas.picking_target = bool(on)
        if on:
            if self.remap_state.selected is None:
                self.remap_pick_hint.setText(
                    "Click a key on the board above to make it the target.")
            else:
                self.remap_pick_hint.setText(
                    f"Now click the key that {self._sel_name()} should send.")
        else:
            self.remap_pick_hint.setText("")
        self._repaint_boards()

    def _sel_name(self):
        """Name of the currently selected key, for the picker hint."""
        idx = self.remap_state.selected
        if idx is None:
            return "this key"
        return KM_KEYS.get(idx, (None, f"key {idx}"))[1]

    def on_remap_mouse(self, func):
        """A mouse function button was pressed (index into MOUSE_FUNCTIONS)."""
        idx = self.remap_state.selected
        if idx is None:
            self.status.setText("pick a key first, then a mouse function")
            return
        label = MOUSE_FUNCTIONS[func][0]
        if self.remap_state.set_word(idx, mouse_word(func)):
            self._repaint_boards()
            self._remap_note(idx)
            self.status.setText(f"{self._sel_name()} -> {label}")

    def _sync_mouse_btns(self, idx):
        """Check the button matching what this key already does.

        Clears the whole group first: a QButtonGroup will not let the last
        button be unchecked by setting the others false, so leaving one lit
        would show the previous key's function as if it belonged to this one.
        """
        word = entry_word(self.remap_state.buf, idx)
        which = split_mouse(word)
        self._mouse_group.setExclusive(False)
        for n, btn in enumerate(self._mouse_btns):
            btn.blockSignals(True)
            btn.setChecked(n == which)
            btn.blockSignals(False)
        self._mouse_group.setExclusive(True)

    def on_remap_media(self, n):
        """A media key button was pressed (index into MEDIA_FUNCTIONS)."""
        idx = self.remap_state.selected
        if idx is None:
            self.status.setText("pick a key first, then a media key")
            return
        if self.remap_state.set_word(idx, media_word(n)):
            self._repaint_boards()
            self._remap_note(idx)
            self.status.setText(
                f"{self._sel_name()} -> {MEDIA_FUNCTIONS[n][0]}")

    def _sync_media_btns(self, idx):
        """Check the media button matching what this key already does.

        Drops exclusivity while syncing: an exclusive QButtonGroup will not
        let its last button be cleared by unticking the others, which would
        leave the previous key's media key lit.
        """
        which = split_media(entry_word(self.remap_state.buf, idx))
        self._media_group.setExclusive(False)
        for n, btn in enumerate(self._media_btns):
            btn.blockSignals(True)
            btn.setChecked(n == which)
            btn.blockSignals(False)
        self._media_group.setExclusive(True)

    def _sync_macro_slot(self, idx):
        """Show whichever macro this key plays, without firing the handler."""
        # A key that plays no macro leaves the box alone: the spin box is also
        # the input, so blanking it to 0 would look like a rebind to macro 0.
        which = split_playmacro(entry_word(self.remap_state.buf, idx))
        if which is None:
            return
        self.remap_macro_slot.blockSignals(True)
        self.remap_macro_slot.setValue(min(which, 15))
        self.remap_macro_slot.blockSignals(False)

    def on_remap_playmacro(self):
        """Bind the selected key to macro slot N, played once."""
        idx = self.remap_state.selected
        if idx is None:
            self.status.setText("pick a key first, then a macro slot")
            return
        slot = self.remap_macro_slot.value()
        if self.remap_state.set_word(idx, playmacro_word(slot)):
            self._repaint_boards()
            self.remap_macro_note.setText(
                f"Slot {slot}, played once. Recorded macros are uploaded with "
                f"Apply macro, and any macro already on the keyboard is "
                f"replaced by that upload.")
            self._remap_note(idx)
            self.status.setText(
                f"{self._sel_name()} -> macro {slot}, played once")

    def on_remap_hotkey(self, n):
        """A hot key button was pressed (index into HOTKEYS)."""
        idx = self.remap_state.selected
        if idx is None:
            self.status.setText("pick a key first, then a shortcut")
            return
        if self.remap_state.set_word(idx, hotkey_word(n)):
            self._repaint_boards()
            self._remap_note(idx)
            self.status.setText(f"{self._sel_name()} -> {HOTKEYS[n][0]}")

    def _sync_hot_btns(self, idx):
        """Check the hot key button matching what this key already does."""
        which = split_hotkey(entry_word(self.remap_state.buf, idx))
        self._hot_group.setExclusive(False)
        for n, btn in enumerate(self._hot_btns):
            btn.blockSignals(True)
            btn.setChecked(n == which)
            btn.blockSignals(False)
        self._hot_group.setExclusive(True)

    def _selected_mods(self):
        """The modifier bitmap the toggles currently describe."""
        mods = 0
        for bit, btn in self.remap_mod_btns.items():
            if btn.isChecked():
                mods |= bit
        return mods

    def _set_mod_btns(self, mods):
        """Show `mods` in the toggles without re-entering the handler."""
        for bit, btn in self.remap_mod_btns.items():
            btn.blockSignals(True)
            btn.setChecked(bool(mods & bit))
            btn.blockSignals(False)

    def on_remap_mods_changed(self):
        """A modifier toggle was clicked: rewrite the word for this key.

        Modifiers are part of the same action word as the target, so changing
        one has to rewrite the entry rather than wait for the key to be picked
        again.
        """
        idx = self.remap_state.selected
        if idx is None:
            return
        usage = self.remap_state.target_usage(idx)
        if usage is None:
            return                      # nothing to add a modifier to
        mods = self._selected_mods()
        if self.remap_state.set_remap(idx, usage, mods):
            self._repaint_boards()
            self._remap_note(idx)
            self.status.setText(
                f"{self._sel_name()} -> "
                f"{modifier_label(mods) + '+' if mods else ''}"
                f"{usage_label(usage)}")

    def on_remap_name(self):
        """A key name was typed into the "or type a key name" box.

        The same choice as clicking the board, made by typing. An ambiguous
        name is refused rather than resolved to whichever key happened to be
        first: "win" matches LWin and RWin, and guessing would silently send
        the wrong key.
        """
        typed = self.remap_name_edit.text()
        hits = usage_matches(typed)
        if not typed.strip():
            self.remap_name_note.setText("")
            return
        if len(hits) == 1:
            usage, label = hits[0]
            self.remap_name_note.setText("")
            self._set_target(usage)
            self.on_remap_usage_picked(usage)
            return
        names = ", ".join(l for _u, l in hits[:4])
        more = "" if len(hits) <= 4 else f" (+{len(hits) - 4} more)"
        if hits:
            self.remap_name_note.setText(
                f"“{typed}” could mean {names}{more} — type one of those.")
        else:
            self.remap_name_note.setText(
                f"No key called “{typed}”. Try a single character (o, 7, /) "
                f"or a name from the board, like F5 or Enter.")

    def on_remap_text_changed(self):
        """Length feedback for the Text box, which cannot be sent yet."""
        n = len(self.remap_text_edit.text())
        if not n:
            self.remap_text_note.setText(
                "Type a string to see how long this would be to send. "
                "Pressing Set action does nothing yet — the keyboard's "
                "encoding for a string has not been decoded, and the app will "
                "not write bytes it cannot verify.")
        else:
            self.remap_text_note.setText(
                f"{n} character{'s' if n != 1 else ''}. Not sent yet: the "
                f"keyboard's encoding for a string has not been decoded.")

    def on_remap_usage_picked(self, usage):
        """The board was clicked to choose a target HID usage."""
        self._set_target(usage)
        idx = self.remap_state.selected
        if idx is None:
            self.status.setText("pick a key first, then a target for it")
            return
        if self.remap_state.set_remap(idx, usage, self._selected_mods()):
            self._repaint_boards()
            self._remap_note(idx)
            mods = self._selected_mods()
            self.status.setText(
                f"{self._sel_name()} -> "
                f"{modifier_label(mods) + '+' if mods else ''}"
                f"{usage_label(usage)}")

    def _set_target(self, usage):
        """Record `usage` as the chosen target and refresh the readout.

        The readout is text, not a widget that lists every usage: the board
        above is where the choice is made, so repeating the catalog here
        would be a 110-row dropdown for no gain.
        """
        self.remap_canvas.target_usage = usage
        if usage is None:
            self.remap_target_readout.setText(
                f"{self._sel_name()} sends whatever it normally sends.")
        else:
            where = ("click it on the board"
                     if usage in _BOARD_USAGES
                     else "not a key on this board")
            self.remap_target_readout.setText(
                f"{self._sel_name()} sends {usage_label(usage)} "
                f"(0x{usage:02x}) — {where}.")
        # The escape hatch combo only owns the off-board usages, so leave it
        # showing the chosen one only while that is what is in effect.
        want = -1 if usage is None else self.remap_offboard.findData(usage)
        if self.remap_offboard.currentIndex() != want:
            self.remap_offboard.blockSignals(True)
            self.remap_offboard.setCurrentIndex(want)
            self.remap_offboard.blockSignals(False)
        return usage

    def on_remap_clear_all(self):
        lid = self.remap_state.layer
        self.remap_state.clear_all()
        self._repaint_boards()
        self._refresh_unsent()
        self.status.setText(
            f"{LAYER_LABEL[lid]} cleared - press Apply to send the change")

    def on_clear_keymaps(self):
        """Reset the key mappings - both layers - and forget the saved ones.

        Deliberately narrow. Clearing the keymap has nothing to do with the
        lighting or the wheel, and wiping those as a side effect would lose work
        the user never asked to lose.
        """
        ret = QMessageBox.question(
            self, "Clear all my key mappings",
            "This puts every key on both layers back to the keyboard's factory "
            "mapping:\n\n"
            "  - the Top layer, the keys you use normally\n"
            "  - the Fn layer, the keys you use while Fn is held\n\n"
            "It also deletes these files from "
            f"{CACHE_DIR}:\n"
            "  - every saved layout\n"
            "  - the last-applied keymap, so it is not restored on next "
            "start\n\n"
            "Your lighting, per-key colours, palettes and the smart wheel are "
            "left exactly as they are.\n\n"
            "This cannot be undone. Nothing is sent to the keyboard until you "
            "press Apply. Continue?")
        if ret != QMessageBox.StandardButton.Yes:
            return
        for layer, buf in self._layer_bufs.items():
            buf[:] = km_layer_default(layer)
        gone = self._clear_keymap_files()
        self._repaint_boards()
        self._refresh_layer_note()
        self._refresh_unsent()
        if self.remap_state.selected is not None:
            self._remap_note(self.remap_state.selected)
        self.status.setText(
            "all key mappings cleared"
            + (f" ({len(gone)} saved file(s) deleted)" if gone else "")
            + " - press Apply to send")

    def _clear_keymap_files(self):
        """Delete only the keymap-related saved state: layouts and the cache.

        Returns the basenames actually removed, so the caller can say how much
        was there. Palettes, custom colours and the wheel cache stay: they are
        not key mappings, and this is a keymap reset.
        """
        gone = []
        try:
            os.remove(KEYMAP_CACHE)
            gone.append(os.path.basename(KEYMAP_CACHE))
        except OSError:
            pass
        # Saved layouts are named by the user, so they need a glob rather than
        # a fixed path. Restricted to this app's own prefix in this directory.
        prefix = "layout_"
        try:
            for name in sorted(os.listdir(CACHE_DIR)):
                if not name.startswith(prefix) or not name.endswith(".bin"):
                    continue
                path = os.path.join(CACHE_DIR, name)
                if not os.path.isfile(path):
                    continue
                os.remove(path)
                gone.append(name)
        except OSError:
            pass
        return gone

    def apply_remap(self):
        """Upload and commit the selected key layer.

        One button, so it always commits. The Fn layer used to be quietly
        downgraded to a staged write because committing it had never been
        tried; it has since been seen on the wire from the OEM app's own reset
        (data/oem_reset_capture.log: `0418 -> 0427 -> 10 pages -> 0402 -> 04F0`),
        so there is no reason to write it differently from the Top layer. What
        is still unverified is whether a layer this app commits survives a power
        cycle, which the layer note says.
        """
        if self._worker and self._worker.isRunning():
            return
        lid = self.remap_state.layer
        payload = bytes(self._layer_bufs[lid])
        self._last_payload = payload
        op = LAYER_OPCODE[lid]
        self.status.setText(f"uploading {LAYER_LABEL[lid].lower()}…")
        self._worker = ApplyWorker(payload, True, opcode=op)
        self._worker.done.connect(self.on_applied)
        self._worker.start()

    def save_remap(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save keymap", "keymap.bin",
                                              "Binary (*.bin)")
        if path:
            open(path, "wb").write(bytes(self.remap_state.buf))
            write_cache(KEYMAP_CACHE, bytes(self.remap_state.buf))
            self.status.setText(f"saved {path}")

    def load_remap(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load keymap", "",
                                              "Binary (*.bin)")
        if not path:
            return
        data = open(path, "rb").read()
        if len(data) != 640:
            QMessageBox.warning(self, "Load", f"expected 640 bytes, got {len(data)}")
            return
        # In place, so the active layer's buffer object is the one that
        # changes. The cache only ever mirrors the Top layer -- there is no
        # separate Fn cache, and inventing one would double the restore path
        # for a region that is not committed anyway.
        self.remap_state.buf[:] = data
        write_cache(KEYMAP_CACHE, bytes(self._layer_bufs[LAYER_TOP]))
        self._repaint_boards()
        self.status.setText(
            f"loaded {os.path.basename(path)} into the "
            f"{LAYER_LABEL[self.remap_state.layer].lower()}")

    # -- named layouts (both layers in one file) --------------------------
    def save_named_layout(self):
        name, ok = QInputDialog.getText(self, "Save layout",
                                        "Name for this layout:")
        if not ok or not name.strip():
            return
        safe = "".join(c for c in name.strip() if c.isalnum() or c in "-_ ").strip()
        if not safe:
            QMessageBox.warning(self, "Save layout",
                                "That name has no usable characters.")
            return
        blob = pack_named_layout(bytes(self._layer_bufs[LAYER_TOP]),
                                 bytes(self._layer_bufs[LAYER_FN]))
        path = os.path.join(CACHE_DIR, f"layout_{safe}.bin")
        if os.path.exists(path):
            ret = QMessageBox.question(
                self, "Save layout",
                f"A layout called '{safe}' is already saved. Replace it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                return
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(path, "wb") as f:
                f.write(blob)
        except OSError as e:
            QMessageBox.warning(self, "Save layout", f"could not write: {e}")
            return
        self.status.setText(f"saved layout '{safe}' ({LAYOUT_FILE_SIZE} bytes)")

    def load_named_layout(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load layout", CACHE_DIR,
                                              "Binary (*.bin)")
        if not path:
            return
        try:
            blob = open(path, "rb").read()
        except OSError as e:
            QMessageBox.warning(self, "Load layout", f"could not read: {e}")
            return
        pair = unpack_named_layout(blob)
        if pair is None:
            QMessageBox.warning(
                self, "Load layout",
                f"That is not a saved layout (expected {LAYOUT_FILE_SIZE} "
                "bytes with the app's header). Use 'Load keymap' for a plain "
                "keymap file.")
            return
        top, fn = pair
        self._layer_bufs[LAYER_TOP][:] = top
        self._layer_bufs[LAYER_FN][:] = fn
        write_cache(KEYMAP_CACHE, top)
        self._show_layer_buf(self.remap_state.layer)
        self.status.setText(
            f"loaded '{os.path.basename(path)}' - both the Top and the Fn "
            f"layer are set")

    # -- helpers -------------------------------------------------------------
    def _repaint(self):
        self.canvas.update()
        self.status.setText(f"checksum 0x{checksum(self.state.buf):04x}")

    def _refresh_controls(self):
        a = self.state.active()
        # Both per-key entries are device mode 0x80, so the row to highlight
        # comes from the palette, not from the active id.
        want = USER2_ID if (a["id"] == USER_ID and self.palette == 1) else a["id"]
        for i in range(self.effect_list.count()):
            if self.effect_list.item(i).data(Qt.ItemDataRole.UserRole) == want:
                self.effect_list.setCurrentRow(i)
                break
        self._sync_color(QColor(a["r"], a["g"], a["b"]))
        # A config saved before brightness became index-based can still hold a
        # raw 0-255 byte, which the firmware reads as black or as out-of-table
        # garbage. Snap it to the nearest usable index so the board is not left
        # dark, and say so rather than doing it silently.
        step = _bri_step(a["bri"])
        sstep = _spd_step(a["spd"])
        heal = []
        if a["bri"] != _BRI_INDEX[step]:
            self.state.set_active(bri=_BRI_INDEX[step])
            heal.append(f"brightness {a['bri']} -> {_BRI_INDEX[step]} "
                        f"({_bri_pct(step)}%)")
        if a["spd"] != _SPD_INDEX[sstep]:
            self.state.set_active(spd=_SPD_INDEX[sstep])
            heal.append(f"speed {a['spd']} -> {_SPD_INDEX[sstep]}")
        for w, v in ((self.full, a["fullcolor"]), (self.bri, step),
                     (self.spd, sstep), (self.dir, a["dir"])):
            w.blockSignals(True)
            if isinstance(w, QCheckBox):
                w.setChecked(bool(v))
            elif isinstance(w, QSlider):
                w.setValue(v)
            else:
                w.setCurrentIndex(1 if v else 0)
            w.blockSignals(False)
        self.bri_label.setText(f"{_bri_pct(step)}%")
        self.spd_label.setText(self._spd_text(sstep))
        self._repaint()  # sets the status to the checksum, so report after it
        if heal:
            self.status.setText("not valid firmware table indices - "
                                + ", ".join(heal))

    def _sync_color(self, col):
        self.current_color = col
        self.swatch.setStyleSheet(
            f"background:{col.name()};border:1px solid #555")
        # keep hex / CMY inputs in sync (guard against feedback loops)
        self._color_sync = True
        self.hex_edit.setText(col.name())
        self.c_spin.setValue(255 - col.red())
        self.m_spin.setValue(255 - col.green())
        self.y_spin.setValue(255 - col.blue())
        self._color_sync = False

    # -- slots ---------------------------------------------------------------
    def on_effect_selected(self, item, _prev=None):
        if item is None:
            return
        eid = item.data(Qt.ItemDataRole.UserRole)
        parked = ""
        if eid in USER_IDS:
            # Same device mode either way; the only difference is which saved
            # per-key palette the region holds.
            want = USER_IDS.index(eid)
            if want != self.palette:
                parked = f", palette {self.palette + 1} saved"
                self._park_palette()
                self.palette = want
                self._load_region(self.palettes[want])
                self._save_palettes()
            self.state.activate(USER_ID)
        else:
            self.state.activate(eid)
        self._refresh_controls()
        a = self.state.active()
        name = dict(EFFECT_LIST).get(eid, f"{a['id']:02x}")
        how = "applies automatically" if self.live_cb.isChecked() \
            else "press Apply to send"
        self.status.setText(
            f"active effect: {_dev_id(eid):02x} {name}{parked} — {how}")
        self._schedule_live_paint()

    def on_key_clicked(self, idx):
        self.canvas.selected = idx
        if self.paint_mode:
            self.state.set_key(idx, self.current_color.red(),
                               self.current_color.green(),
                               self.current_color.blue())
            self.canvas.update()
            self.status.setText(f"painted {_light_name(idx)} "
                                f"{self.current_color.name()} (checksum 0x"
                                f"{checksum(self.state.buf):04x})")
            self._schedule_live_paint()
        else:
            c = self.state.key_color(idx)
            if c:
                self._sync_color(QColor(*c))
            self.canvas.update()

    def on_key_right(self, idx):
        self.state.clear_key(idx)
        self.canvas.update()
        self.status.setText(f"cleared {_light_name(idx)}")
        self._schedule_live_paint()

    def pick_color(self):
        # Non-native dialog: the plain picker without a custom-colours section;
        # custom colours live in the Color column instead.
        dlg = QColorDialog(self.current_color, self)
        dlg.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog, True)
        dlg.setWindowTitle("Effect color")
        if dlg.exec():
            col = dlg.currentColor()
            if col.isValid():
                self.set_color(col)

    def set_color(self, col):
        self._sync_color(col)
        if self.state.active()["id"] != USER_ID:
            self.state.set_active(r=col.red(), g=col.green(), b=col.blue())
            self._repaint()
            self._schedule_live_paint()
            self.status.setText(
                f"paint color {col.name()} — sending to the board")
        else:
            # Nothing to write, and it used to say "paint color <name>", which
            # reads as though the board had just been painted. It had not: in
            # User Defined the active block is inert and the per-key region is
            # what the board renders, so the colour only reaches a key when a
            # Zone is clicked. Say that, and name the thing that works.
            self.status.setText(
                f"color set to {col.name()}. User Defined paints each key on "
                f"its own — click a Zone, or All, to paint it")

    def on_hex_changed(self):
        if getattr(self, "_color_sync", False):
            return
        hx = self.hex_edit.text().strip()
        if hx and not hx.startswith("#"):
            hx = "#" + hx
        col = QColor(hx)
        if col.isValid() and len(hx) == 7:
            self.set_color(col)

    def on_cmy_changed(self, *_):
        if getattr(self, "_color_sync", False):
            return
        self.set_color(QColor(255 - self.c_spin.value(),
                              255 - self.m_spin.value(),
                              255 - self.y_spin.value()))

    def store_custom(self, i):
        c = self.current_color
        self.custom_colors[i] = QColor(c)
        self.custom_btns[i].setStyleSheet(
            f"background:{c.name()};border:1px solid #333")
        buf = bytearray(24)
        for j, col in enumerate(self.custom_colors):
            buf[j * 3] = col.red()
            buf[j * 3 + 1] = col.green()
            buf[j * 3 + 2] = col.blue()
        write_cache(CUSTOM_COLORS_FILE, bytes(buf))
        self.status.setText(f"saved color to custom slot {i}")

    def _refresh_custom(self):
        for j, col in enumerate(self.custom_colors):
            self.custom_btns[j].setStyleSheet(
                f"background:{col.name()};border:1px solid #333")

    def on_full(self, st):
        self.state.set_active(fullcolor=1 if st else 0)
        self._repaint()
        self._schedule_live_paint()

    def on_bri(self, step):
        self.state.set_active(bri=_BRI_INDEX[step])
        self.bri_label.setText(f"{_bri_pct(step)}%")
        self._repaint()
        self._schedule_live_paint()

    def on_spd(self, step):
        self.state.set_active(spd=_SPD_INDEX[step])
        self.spd_label.setText(self._spd_text(step))
        self._repaint()
        self._schedule_live_paint()

    @staticmethod
    def _spd_text(step):
        # period (ec*c9)/400, so the biggest c9 is the slowest animation
        return "Slowest" if step == 0 else (
            "Fastest" if step == len(_SPD_INDEX) - 1 else "")

    def on_dir(self, i):
        self.state.set_active(dir=i)
        self._repaint()
        self._schedule_live_paint()

    def on_paint_toggle(self, st):
        self.paint_mode = bool(st)
        if st and self.state.active()["id"] != USER_ID:
            self.state.activate(USER_ID)
            self._refresh_controls()
            self._schedule_live_paint()

    def on_clear_keys(self):
        self.state.clear_all_keys()
        self.canvas.update()
        self.status.setText("per-key region cleared")
        self._schedule_live_paint()

    # -- zones ----------------------------------------------------------------
    def _zone_idx(self):
        return max(0, self.zone_list.currentRow())

    def _zone_cardinality(self, i=None):
        return len(ZONES[self._zone_idx() if i is None else i][1])

    def _zone_name(self):
        return ZONES[self._zone_idx()][0]

    def _zone_unit(self, idxs):
        """'29 strips', '115 keys', or '115 keys + 29 strips'."""
        ns = sum(1 for i in idxs if i in _STRIP_BY_ENTRY)
        nk = len(idxs) - ns
        if not nk:
            return f"{ns} strips"
        if not ns:
            return f"{nk} keys"
        return f"{nk} keys + {ns} strips"

    def on_zone_apply(self, *_):
        r, g, b = (self.current_color.red(), self.current_color.green(),
                   self.current_color.blue())
        idxs = ZONES[self._zone_idx()][1]
        self.zone_list.setCurrentRow(self._zone_idx())
        self.state.activate(USER_ID)   # per-key painting only renders in 0x80
        self._refresh_controls()       # sync effect list / params to 0x80
        self._sync_color(QColor(r, g, b))   # keep the picked color for repeats
        for idx in idxs:
            self.state.set_key(idx, r, g, b)
        name = self._zone_name()
        unit = self._zone_unit(idxs)
        self.zone_note.setText(
            f"{name}: {unit} → "
            f"{self.current_color.name()} (active effect = User Defined)")
        self.canvas.update()
        msg = f"zone {name}: painted {unit}"
        if any(i in _STRIP_BY_ENTRY for i in idxs):
            msg += "  (strips need firmware.strip28.bin)"
        self.status.setText(msg)
        self._schedule_live_paint()

    def on_zone_clear(self, *_):
        name = self._zone_name()
        idxs = ZONES[self._zone_idx()][1]
        self.zone_list.setCurrentRow(self._zone_idx())
        self.state.activate(USER_ID)
        for idx in idxs:
            self.state.clear_key(idx)
        unit = self._zone_unit(idxs)
        self.zone_note.setText(f"{name}: cleared {unit}")
        self.canvas.update()
        self.status.setText(f"zone {name}: cleared {unit}")
        self._schedule_live_paint()

    def restore_default(self):
        self.state.buf = load_default_buf()
        self._resync_palette()
        self._refresh_controls()
        how = "applies automatically" if (getattr(self, "live_cb", None)
                                          and self.live_cb.isChecked()) \
            else "press Apply to send"
        self.status.setText(f"restored OEM default ({how})")
        self._schedule_live_paint()

    # -- device I/O ----------------------------------------------------------
    def _schedule_live_paint(self):
        """Arrange a debounced commit-upload of the current lighting config.

        Fired after every edit (and once on app open after the state read).
        The 0.45 s debounce means a slider drag or zone paint coalesces into a
        single upload. If an upload is already running, `_live_pending` stays
        set and `on_applied` re-arms the timer so the change is not dropped.
        """
        if not getattr(self, "live_cb", None) or not self.live_cb.isChecked():
            return
        self._live_pending = True
        self._live_timer.start()

    def _live_timer_fired(self):
        if self._live_pending:
            self._live_pending = False
            self.apply(live=True)

    def on_live_toggled(self, on):
        if not on:
            self._live_timer.stop()
            self._live_pending = False
        else:
            self.status.setText("live paint on: changes apply as you edit")

    def apply(self, live=False):
        """Upload and commit the lighting config.

        There is only one button now: a staged write without the 0x04F0 commit
        was never something the user asked for, and offering it next to the
        real one invited choosing the one that does not survive a power cycle.
        """
        if self._worker and self._worker.isRunning():
            # The button path should never land here (it is disabled while a
            # worker runs). A live edit arriving mid-upload keeps pending so
            # `on_applied` re-arms the timer and the newest state is pushed.
            if live:
                self._live_pending = True
            return
        payload = bytes(self.state.buf)
        self._last_payload = payload
        self.apply_btn.setEnabled(False)
        self.status.setText("live paint: uploading…" if live else "uploading…")
        self._worker = ApplyWorker(payload, True)
        self._worker.done.connect(self.on_applied)
        self._worker.start()

    def on_applied(self, ok, msg):
        self.apply_btn.setEnabled(True)
        self.status.setText(("OK: " if ok else "FAILED: ") + msg)
        if ok:
            op = getattr(self._worker, "opcode", OP_COLORS)
            # A key layer the keyboard accepted is now what we believe it
            # holds, so its unsent marker clears. Done before the cache
            # bookkeeping below, which would otherwise swallow the keymap
            # opcode in its own branch. Only on success: a refused session
            # persists nothing, so its marker has to stay.
            if op in LAYER_KEYMAP_TO_LAYER:
                self._layer_now_sent(LAYER_KEYMAP_TO_LAYER[op])
            if op == OP_COLORS and len(self._last_payload) == 1152:
                write_cache(COLORS_CACHE, self._last_payload)
            elif op == OP_WRITE_KEYMAP and len(self._last_payload) == 640:
                write_cache(KEYMAP_CACHE, self._last_payload)
            elif op == OP_WHEEL and len(self._worker.payload) == 64:
                # The wheel page is whole-region like the keymap; cache
                # the payload the device just accepted, not the editor.
                write_cache(WHEEL_CACHE, self._worker.payload)
        # A live edit arrived while the previous upload was still running:
        # re-arm so the newest state gets pushed too.
        if getattr(self, "_live_pending", False):
            self._live_timer.start()

    def save_file(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save config", "config.bin",
                                              "Binary (*.bin)")
        if path:
            open(path, "wb").write(bytes(self.state.buf))
            write_cache(COLORS_CACHE, bytes(self.state.buf))
            self.status.setText(f"saved {path}")

    def load_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load config", "",
                                              "Binary (*.bin)")
        if not path:
            return
        data = open(path, "rb").read()
        if len(data) != 1152:
            QMessageBox.warning(self, "Load", f"expected 1152 bytes, got {len(data)}")
            return
        self.state.buf = bytearray(data)
        # Snap before the cache write, so loading a file from an older build
        # cannot re-persist the very raw values we just repaired.
        fixed = self.state._normalize_params()
        write_cache(COLORS_CACHE, bytes(self.state.buf))
        self._resync_palette()
        self._refresh_controls()
        how = "applies automatically" if (getattr(self, "live_cb", None)
                                          and self.live_cb.isChecked()) \
            else "press Apply to send"
        self.status.setText(f"loaded {path} ({how})" + (
            f" (repaired {fixed} brightness/speed value(s))" if fixed else ""))
        self._schedule_live_paint()


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.resize(1250, 720)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
