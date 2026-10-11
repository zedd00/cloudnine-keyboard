"""Global key capture for the macro recorder.

The macro record format is already decoded (cloudnine_keymap.macro_record:
8 bytes per event, a HID usage, a down/up flag and a LE16 delay in ms), so
recording is plumbing rather than further reverse engineering. This module
supplies the missing plumbing: it watches the system's keyboards through evdev
and turns what you type into macro events.

Two things about the format drive the design here.

**The delay is a hold, waited after its record, not a gap before it, and it
counts milliseconds.** See TIMING_UNIT_MS below for the evidence. In the
OEM captures (data/oem_playmacro_capture.log) most records hold 0x000a, but slot
1 holds [127, 641, 111, 1832, 112, 19] -- a 1.8 s figure is not a fixed 10 ms
tick, so it is a real duration.

Which event it belongs to is settled by the OEM's own fixed-10 ms macro. Read as
a hold-after, every key is held exactly 10 ms and the trailing record carries
the leftover wait. Read as a wait-before, that same macro ends with the final
key down immediately followed by its release, so the last key would never
register. Hold-after is the only reading under which the vendor's shipped
feature types its last character.

Two consequences for recording:

- An event's hold is only known once the *next* event arrives, so each record is
  back-dated when the following one lands. The final event's delay stays 0 -- nothing follows it, and the OEM writes 0 there.
- There is no field in front of the first event, so the pause before the user
  starts typing is discarded automatically. An earlier version stored that pause
  as the first record's delay; with hold-after semantics that held the *first
  key* down for the length of the hesitation (5 s of thinking -> a 5 s stall),
  and setting it to 0 instead held the first key for 0 ms so it never registered
  at all and the macro appeared dead.

**Keys are recorded as ordinary usages.** The captured macro for "1 Q W LShift"
holds 0xE1 for LShift exactly like any other key, so modifiers need no special
case -- they are just press/release pairs like everything else.

Only keys whose USB HID usage this repo already knows about are recorded, minus
the Windows keys, which are dropped on purpose (see SKIP_USAGES). A key with no
confident mapping (media keys, the consumer page, the numpad) is *not* guessed
at: it is counted and reported so the UI can say what it dropped rather than
writing a byte nobody has ever seen the firmware play.

The evdev import is deferred to first use, so the GUI still starts on a machine
without python-evdev installed; callers check available() first.
"""

import os
import threading
import time

from cloudnine_keymap import HID_BY_USAGE, usage_label

# evdev import is optional. `available()` reports whether recording is possible
# so the GUI can disable its button instead of failing at click time.
evdev = None
try:                                    # pragma: no cover - import guard
    import evdev as _evdev
    evdev = _evdev
except ImportError:                     # pragma: no cover
    pass


def available() -> bool:
    """True if global key capture is possible on this machine."""
    return evdev is not None


# A recorded macro must fit in the region, and the slot pointer is a 16-bit
# offset. 512 events is 4 KB of records -- far more than the OEM app ever wrote
# and enough for any sane macro -- and keeps a forgotten recording from turning
# into an unbounded buffer.
MAX_EVENTS = 512

# How often the capture loop looks for a keyboard that was plugged in after
# recording started. USB keyboards do turn up mid-session.
RESCAN_S = 2.0


def _letter_usages():
    """{Linux keycode: HID usage} for the 26 letter keys.

    Derived from evdev's own name table rather than arithmetic on the code:
    Linux keycodes for the three letter rows are not contiguous (KEY_LEFTSHIFT
    is 42, inside the 30..44 run that reads A..Z), so 'A + code - 30' silently
    turns LShift into 'M' and shifts every letter after it.
    """
    out = {}
    for code in evdev.ecodes.keys:
        name = _name_of(code)
        if name.startswith("KEY_") and len(name) == 5 and name[4].isalpha():
            out[code] = 0x04 + (ord(name[4].lower()) - ord("a"))
    return out


def _name_of(code: int) -> str:
    """evdev's canonical name for a keycode.

    evdev stores aliases as a tuple of names (0x71 is both KEY_MIN_INTERESTING
    and KEY_MUTE), so the table needs normalising before any string test.
    """
    if evdev is None:
        return f"code {code}"
    name = evdev.ecodes.keys.get(code, f"code {code}")
    if isinstance(name, (tuple, list)):
        name = name[0]
    return name


# Keys that are not letters, by evdev name -> USB HID Keyboard page usage.
# Every value here is cross-checked against cloudnine_keymap.HID_BY_USAGE by
# tools/check_recorder.py, so a typo cannot become an unplayable record.
_HID_BY_NAME = {
    "KEY_ESC": 0x29,
    "KEY_1": 0x1E, "KEY_2": 0x1F, "KEY_3": 0x20, "KEY_4": 0x21,
    "KEY_5": 0x22, "KEY_6": 0x23, "KEY_7": 0x24, "KEY_8": 0x25,
    "KEY_9": 0x26, "KEY_0": 0x27,
    "KEY_MINUS": 0x2D, "KEY_EQUAL": 0x2E,
    "KEY_BACKSPACE": 0x2A, "KEY_TAB": 0x2B,
    "KEY_LEFTBRACE": 0x2F, "KEY_RIGHTBRACE": 0x30, "KEY_BACKSLASH": 0x31,
    "KEY_ENTER": 0x28, "KEY_SPACE": 0x2C, "KEY_CAPSLOCK": 0x39,
    "KEY_SEMICOLON": 0x33, "KEY_APOSTROPHE": 0x34, "KEY_GRAVE": 0x35,
    "KEY_COMMA": 0x36, "KEY_DOT": 0x37, "KEY_SLASH": 0x38,
    "KEY_LEFTCTRL": 0xE0, "KEY_LEFTSHIFT": 0xE1,
    "KEY_LEFTALT": 0xE2, "KEY_LEFTMETA": 0xE3,
    "KEY_RIGHTCTRL": 0xE4, "KEY_RIGHTSHIFT": 0xE5,
    "KEY_RIGHTALT": 0xE6, "KEY_RIGHTMETA": 0xE7,
    "KEY_F1": 0x3A, "KEY_F2": 0x3B, "KEY_F3": 0x3C, "KEY_F4": 0x3D,
    "KEY_F5": 0x3E, "KEY_F6": 0x3F, "KEY_F7": 0x40, "KEY_F8": 0x41,
    "KEY_F9": 0x42, "KEY_F10": 0x43, "KEY_F11": 0x44, "KEY_F12": 0x45,
    "KEY_SYSRQ": 0x46, "KEY_SCROLLLOCK": 0x47, "KEY_PAUSE": 0x48,
    "KEY_INSERT": 0x49, "KEY_HOME": 0x4A, "KEY_PAGEUP": 0x4B,
    "KEY_DELETE": 0x4C, "KEY_END": 0x4D, "KEY_PAGEDOWN": 0x4E,
    "KEY_RIGHT": 0x4F, "KEY_LEFT": 0x50, "KEY_DOWN": 0x51, "KEY_UP": 0x52,
}

# Filled in by hid_usage_table() on first use, so importing this module does not
# require evdev. codes -> HID usage.
_USAGE_BY_CODE = {}


def hid_usage_table():
    """{Linux keycode: HID usage} for every key this recorder can capture.

    Returns an empty dict when evdev is missing; callers should have checked
    available() first.
    """
    if not _USAGE_BY_CODE and evdev is not None:
        table = _letter_usages()
        for name, usage in _HID_BY_NAME.items():
            code = evdev.ecodes.ecodes.get(name)
            if code is not None:
                table[code] = usage
        # Only usages the repo's own board catalog knows about are worth
        # emitting. Anything outside it has never been seen the firmware play.
        _USAGE_BY_CODE.update(
            {c: u for c, u in table.items()
             if u in HID_BY_USAGE and u not in SKIP_USAGES})
    return _USAGE_BY_CODE


def key_name(code: int) -> str:
    """evdev's name for a keycode, for showing what was dropped."""
    return _name_of(code)


def _open(path: str):
    """Open `path` for inspection, or None if it cannot be read.

    One open per path, deliberately. The initial scan over /dev/input walks
    every event node, and an earlier version opened each one twice, which
    pushed the scan past the moment the first device appeared -- so a recorder
    could start out watching nothing (2026-10-03).
    """
    if evdev is None:
        return None
    try:
        return evdev.InputDevice(path)
    except OSError:
        return None


def is_keyboard(path: str) -> bool:
    """True if `path` is a device worth recording from.

    A keyboard has EV_KEY and an alphabetic key. That is a cheap, sufficient
    test and it keeps mice, lid switches and power buttons out. Every keyboard
    qualifies, the board this app configures included.
    """
    dev = _open(path)
    if dev is None:
        return False
    try:
        caps = dev.capabilities()
        if evdev.ecodes.EV_KEY not in caps:
            return False
        return evdev.ecodes.KEY_A in caps[evdev.ecodes.EV_KEY]
    except (OSError, AttributeError):
        return False
    finally:
        dev.close()


def input_device_paths():
    """Every readable event device under /dev/input, in name order."""
    import glob
    return sorted(glob.glob("/dev/input/event*"))


class MacroRecorder:
    """Collects key presses into one macro's event list.

    Usage is start() -> (let the user type) -> stop(), which returns the event
    list ready for cloudnine_keymap.build_macro_region(). Events are also
    available live through snapshot() so a UI can show progress.

    The capture loop runs on a plain thread with a lock, so a GUI can poll it
    with a timer and never touch a Qt signal from that thread.
    """

    def __init__(self, max_events: int = MAX_EVENTS, fixed_ms=None):
        self.max_events = max_events
        # When set, every hold is this many milliseconds instead of the real
        # one. Setting it to 10 reproduces the OEM app's own FIXED-delay
        # recording mode -- every record 0x000a, the last 0x0000 -- which is
        # confirmed to play back correctly on the keyboard.
        self.fixed_ms = fixed_ms
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._devices = {}
        self._events = []
        self._unmapped = {}
        self._truncated = False
        self._error = None

    # -- state ------------------------------------------------------------
    @property
    def running(self) -> bool:
        """True between a successful start() and the matching stop().

        Checks the thread rather than just the flag: the flag is cleared
        internally when the capture loop dies on an exception, so the thread
        being gone is the honest signal that nothing is being recorded.
        """
        thread = self._thread
        return thread is not None and thread.is_alive()

    def snapshot(self):
        """(events, unmapped, truncated) as of right now, without stopping."""
        with self._lock:
            return (list(self._events), dict(self._unmapped), self._truncated)

    def events(self):
        """The macro recorded so far."""
        return self.snapshot()[0]

    def unmapped(self):
        """{evdev name: times pressed} for keys that were NOT recorded."""
        return self.snapshot()[1]

    def truncated(self) -> bool:
        """True if the recording hit max_events and dropped the rest."""
        return self.snapshot()[2]

    def error(self):
        """The exception that ended capture early, or None.

        Recording that stops on its own is a failure the caller has to hear
        about, otherwise a broken recorder is indistinguishable from a quiet
        keyboard.
        """
        return self._error

    def device_paths(self):
        """The keyboards currently being read.

        A keyboard plugged in after recording started only appears once the
        capture loop rescans (RESCAN_S), so a caller that wants to prompt the
        user should compare this against its own device list.
        """
        return sorted(self._devices)

    def describe(self, limit: int = 8) -> str:
        """A one-line summary of the current recording, for a live label."""
        events = self.events()
        if not events:
            return "nothing yet -- press some keys"
        keys = [usage_label(u) for u, down, *_ in events if down]
        head = " ".join(keys[:limit])
        return head + (f" +{len(keys) - limit}" if len(keys) > limit else "")

    # -- run --------------------------------------------------------------
    def start(self):
        """Begin capturing. Raises RuntimeError if capture is unavailable."""
        if not available():
            raise RuntimeError(
                "python-evdev is not installed, so keys cannot be captured. "
                "Install it with: pip install evdev")
        with self._lock:
            self._events = []
            self._unmapped = {}
            self._truncated = False
            self._error = None
            self._devices = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop capturing and return the macro's event list.

        Returns [] if nothing was recorded. Stops are safe to call twice.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None
        for dev in list(self._devices.values()):
            try:
                dev.close()
            except OSError:
                pass
        self._devices = {}
        return self.events()

    # -- internals --------------------------------------------------------
    def _open_keyboards(self):
        for path in input_device_paths():
            if path in self._devices:
                continue
            if not is_keyboard(path):
                continue
            try:
                self._devices[path] = evdev.InputDevice(path)
            except OSError:
                continue

    def _close_gone(self):
        """Drop devices whose /dev node has gone, e.g. an unplugged keyboard."""
        for path in list(self._devices):
            if os.path.exists(path):
                continue
            try:
                self._devices.pop(path).close()
            except (OSError, KeyError):
                pass

    def _record(self, code: int, down: bool):
        """Add one key event, and back-date the previous one's hold.

        `delay[i]` is how long to HOLD event i, waited after it -- not the gap
        before it. The OEM's own fixed-10 ms macro proves it: read as
        hold-after, every key is held exactly 10 ms and the trailing record
        carries the leftover wait. Read as wait-before, that same macro ends
        with the final key down immediately followed by its release, so the
        last key would never register.

        So the hold of an event is only known once the NEXT event arrives, which
        is why the previous record is rewritten here rather than appended with
        a guess. The last event's delay stays 0: nothing follows it, and the OEM
        writes 0 there in its fixed-delay recordings.

        Nothing goes in front of the first event either, which is what discards
        the user's pause before starting to type -- there is no field for it.
        """
        with self._lock:
            if len(self._events) >= self.max_events:
                self._truncated = True
                return
            now = time.monotonic()
            if self._events:
                prev = self._events[-1]
                if self.fixed_ms is not None:
                    hold = self.fixed_ms
                else:
                    hold = int(round((now - prev[3]) * 1000))
                hold = max(0, min(0xFFFF, hold))
                self._events[-1] = (prev[0], prev[1], hold, prev[3])
            # The 4th field is the raw timestamp, kept only to measure the hold
            # of the event that follows. build_macro_region() uses the first
            # three.
            self._events.append((self._usage(code), down, 0, now))

    def _usage(self, code: int):
        """HID usage for a code, counting it as dropped when unmapped."""
        table = hid_usage_table()
        usage = table.get(code)
        if usage is None:
            with self._lock:
                name = key_name(code)
                self._unmapped[name] = self._unmapped.get(name, 0) + 1
            return None
        return usage

    def _loop(self):
        # Wrapped so a failure ends recording visibly. An unguarded loop body
        # dies quietly on the first exception and the UI keeps showing an empty
        # "recording", which is indistinguishable from the user not typing.
        try:
            self._run()
        except Exception as e:             # noqa: BLE001
            with self._lock:
                self._error = e
            self._stop.set()

    def _run(self):
        import select
        self._open_keyboards()
        next_scan = time.monotonic() + RESCAN_S
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_scan:
                self._open_keyboards()
                self._close_gone()
                next_scan = now + RESCAN_S
            fds = [d.fd for d in self._devices.values()]
            if not fds:
                self._stop.wait(0.25)
                continue
            try:
                ready, _, _ = select.select(fds, [], [], 0.25)
            except (OSError, ValueError):
                # A device went away between the fd list and the select.
                self._close_gone()
                continue
            for path, dev in list(self._devices.items()):
                if dev.fd not in ready:
                    continue
                try:
                    events = dev.read()
                except (OSError, BlockingIOError):
                    continue
                for ev in events:
                    if ev.type != evdev.ecodes.EV_KEY:
                        continue
                    if ev.value == 2:
                        # Auto-repeat. The OEM captures hold no repeat records,
                        # so a held key is one press and one release here too.
                        continue
                    self._consume(ev)

    def _consume(self, ev):
        usage = hid_usage_table().get(ev.code)
        if usage is None:
            # Only note a dropped key once, on press, so a held key does not
            # inflate the count.
            if ev.value == 1:
                self._usage(ev.code)
            return
        if ev.value not in (0, 1):
            return
        self._record(ev.code, ev.value == 1)


# The delay in a macro record is NOT milliseconds. See TIMING_UNIT_MS below.
#
# **MEASURED ON HARDWARE 2026-10-10: the firmware waits about 5.3x longer than
# the number written.** `tools/watch_macro.py` recorded a macro of `A S D F` and
# timed the keyboard's own output. Five consecutive records, measured against
# what was stored in each:
#
#     stored    58   210    59   260   106   (ms)
#     waited   312   1087  312  1389   554   (ms)
#     ratio   5.38  5.18  5.29  5.34  5.22
#
# Five independent delays, one consistent factor. So the unit is not 1 ms and
# not 10 ms; it is a little over 5 ms per unit. 5.28 is used rather than a round
# 5 because the measurement says 5.28 and rounding down would make every macro
# drift long.
#
# **This reverses two earlier conclusions, both of which were reasoned rather
# than measured.** The first pass read the OEM's own `alttests` capture (delays
# of 752/168/2968/504/3904/664/168/45/3612) and concluded the unit was 1 ms
# because the values looked like ordinary hand pauses. They did -- but that macro
# has never been watched playing, and on a 5.28x unit it plays 69 seconds, which
# is exactly the kind of macro that never finishes.
TIMING_UNIT_MS = 5.28

# Playback gets cut short, and whatever key was down when it stopped stays
# down -- which is what produced the runs of repeated characters.
#
# The cutoff is a **budget on total playback time, not a key or record count.**
# Measured 2026-10-10: a macro of `A S D F` stopped after its third key, 3654 ms
# in. Its fourth key needed another 2013 ms of wait before it would even have
# started, so playback would have run to 5667 ms. The OEM app's own `asdzxcQWE`
# -- 24 records, which plays in full -- is 1272 ms on this unit, comfortably
# inside. Every macro this app recorded was hand-timed and therefore far longer,
# so every one of them ran out of budget part-way.
#
# The old wording blamed a stuck key. The stuck key is the *consequence*; the
# cause is the budget, and CUTOFF_BUDGET_MS says so.
CUTOFF_BUDGET_MS = 4000
CUTOFF_NOTE = ("playback ran out of time part-way, so the last key stayed held "
               "and the operating system auto-repeated it")

# The same threshold as before, now in real milliseconds.
# A warning threshold, in the same units. The firmware appears to give up on a
# macro that runs long -- a real-timing recording was cut off after its first
# key -- so this is reported rather than silently uploaded.
LONG_HOLD_MS = 400

# Modifier usages, split by what holding one across a key actually MEANS.
#
# Shift is part of ordinary typing: Shift+letter is a capital, and the OEM app's
# own "asdzxcQWE" macro holds it across three keys and plays back perfectly.
#
# Ctrl and Alt turn the same gesture into a desktop SHORTCUT.
# The window manager sees the modifier before the keyboard does, so those keys
# never reach a text field: Alt+Tab switches windows, Alt+F4 closes whatever
# has focus. A macro that holds one across another key will not type what it
# looks like it should, and that is the desktop's doing rather than the
# keyboard's -- no arrangement of the macro bytes changes it.
SHIFT_LIKE = frozenset({0xE1, 0xE5})        # LShift, RShift
COMMAND_LIKE = frozenset({0xE0, 0xE2,        # LCtrl, LAlt
                          0xE4, 0xE6})       # RCtrl, RAlt (RAlt is also AltGr)
MOD_USAGES = SHIFT_LIKE | COMMAND_LIKE

# The Windows keys are not recorded at all. They are dropped from the usage
# table so they arrive through the existing "not captured" path and are counted
# on screen, rather than being silently swallowed.
#
# The OEM app's own "alttests" macro ends on an LWin key-down with no release
# after it (data/oem_alt_capture.log), so replaying one leaves the Windows key
# held down and every later keystroke on that computer becomes a Windows
# shortcut. Asked to skip them on 2026-10-04.
SKIP_USAGES = frozenset({0xE3, 0xE7})        # LWin, RWin


def recording_warnings(events, limit: int = 4):
    """Things about a recording that will not replay the way it was typed.

    A recorder that quietly captures the wrong events is worse than one that
    refuses: the macro uploads cleanly, the device accepts it, and the only
    symptom is nonsense coming out of the key. So the anomalies are named.

    Returns a list of human strings, empty when nothing looks wrong.
    """
    out = []
    # The firmware gives up on a macro that runs long, so the thing worth
    # reporting is the TOTAL, not any single record. Measured 2026-10-10: an
    # `A S D F` macro stopped after its third key at 3654 ms of playback.
    total_ms = sum(d for _u, _down, d in events) * TIMING_UNIT_MS
    if total_ms > CUTOFF_BUDGET_MS:
        out.append(
            f"this macro will take about {total_ms:.0f} ms to play and the "
            f"keyboard gives up near {CUTOFF_BUDGET_MS} ms, so it will stop "
            f"part-way and leave the last key held, which auto-repeats. "
            f"Recorded with the hand pauses included it was {total_ms:.0f} ms; "
            f"uncheck fixed timing, or use the fixed-delay option, to bring it "
            f"inside the budget.")
    long_holds = [(i, d) for i, (_u, down, d) in enumerate(events)
                  if d > LONG_HOLD_MS]
    if long_holds:
        worst = max(long_holds, key=lambda p: p[1])
        out.append(
            f"{len(long_holds)} record(s) hold over {LONG_HOLD_MS} ms "
            f"(longest {worst[1]} ms). Any hold past about 250 ms is long "
            f"enough for the operating system to auto-repeat the key, so it "
            f"will type a burst of that character rather than one.")
    # Two keys down at once, or a key released that never went down.
    #
    # Shift is exempt from the chord check, because holding Shift across a
    # letter is how a capital is typed -- the OEM app's own "asdzxcQWE" macro
    # does it three times and plays back in full. What is worth reporting is two
    # ordinary keys overlapping, a command-like modifier held across another
    # key, and any key left down at the end.
    command_held = None
    for i, (usage, down, _d) in enumerate(events):
        if usage in COMMAND_LIKE:
            command_held = usage if down else None
        elif down and command_held is not None and usage not in MOD_USAGES:
            out.append(
                f"{usage_label(command_held)} is held at record {i} while "
                f"{usage_label(usage)} is pressed. Ctrl or Alt across a key is "
                f"a desktop shortcut, not a character: the window manager acts "
                f"on it instead of the letter reaching anything. Pressed as a "
                f"shortcut on purpose that is just what you wanted; if you "
                f"meant the letter to be typed, this part will not do it.")
            break
    held_keys = []
    for usage, down, _d in events:
        if usage in MOD_USAGES:
            continue
        if down and held_keys:
            out.append(
                f"{usage_label(held_keys[-1])} is still held when "
                f"{usage_label(usage)} is pressed, at record "
                f"{len(held_keys)} keys in. Two ordinary keys at once is a "
                f"chord, and macros replay it as two overlapping keys rather "
                f"than in sequence.")
            break
        if down:
            held_keys.append(usage)
        elif held_keys and held_keys[-1] == usage:
            held_keys.pop()
    # Whatever is left in held_keys was pressed and never released. NOT "every
    # press": an earlier version listed all of them and claimed the OEM's own
    # 16-record macro left F8 held down when nothing was wrong.
    for usage in held_keys:
        out.append(
            f"{usage_label(usage)} is pressed but never released in this "
            f"recording, so it would stay held down for the rest of the macro.")
    # A modifier left down is worse than an ordinary key: the OEM app's own
    # "alttests" macro leaves keys out (data/oem_alt_capture.log), and anything
    # still held colours every keystroke after it.
    stuck_mod = [u for u, down, _d in events
                 if u in MOD_USAGES and down
                 and not any(v and not d for v, d, _x in events
                             if v == u)]
    for usage in stuck_mod:
        out.append(
            f"{usage_label(usage)} goes down and never comes back up, so the "
            f"whole rest of the macro would be sent with it held -- and "
            f"anything typed afterwards on the computer would too.")
    # Modifiers are FINE. An earlier warning here was wrong: it was inferred
    # from a macro that stopped after three keys, which turned out to be the
    # short-region problem (see MACRO_REGION_MIN). The OEM app's own
    # "asdzxcQWE" macro -- nine characters with three Shift presses between
    # them, 24 records -- plays back in full (data/oem_macro6_capture.log,
    # 2026-10-03).
    return out


def strip_stamps(events):
    """Drop the capture-loop timestamps, leaving what build_macro_region wants."""
    return [(u, down, delay) for u, down, delay, _t in events]