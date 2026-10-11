# Cloudnine ErgoFSK C989 — keyboard software and firmware

This folder is the release. It has two parts:

```
application/     the Python app (lighting, wheel, key remapping, macros)
firmware/        the flasher and the two firmware images
```

---

## 1. What the application does

It talks to the keyboard directly over USB, so it replaces the vendor's
Windows application once the firmware is in place.

| Tab | What it does |
|---|---|
| **Lighting** | Key colours, lighting effects, colour zones, per-key painting, a live preview, and save/load of your own presets. |
| **Smart wheel** | What each of the four detents of the thumb wheel does (colour, play/pause, and so on), and the ring colour. |
| **Key remap** | Change what any key sends: another key, a key combo, a mouse button, a named shortcut, a media key, a macro, or nothing at all. Whole-layout presets (QWERTY, Colemak, Dvorak, Qwertz, Azerty, Mac) move letters *and* punctuation in one click. Two layers: the normal one and the Fn layer. |
| **Macros** | Record a macro from any keyboard, read back and hand-edit the events, keep a named library, and bind it to a key — including the M1–M10 keys. |
| **Firmware** | Flash the stock or the modified firmware (see below). |

There is also a command-line tool, `application/cloudnine_app.py`, for
scripting and for poking at things the GUI does not show.

Nothing is sent to the keyboard until you press an **Apply** button.

---

## 2. Read this before you flash anything

**The two firmware images behave differently, and the differences are not
symmetrical.** Pick deliberately.

|  | `firmware.oem.bin` (stock) | `firmware.v1.0.bin` (ours) |
|---|---|---|
| Factory behaviour | exactly as the keyboard shipped | modified |
| The **M10 / Light** key | **swallowed into the lighting colour cycle** — it will not type anything | silent until you assign it on the Key remap tab |
| Wheel ring colour | stock behaviour | follows **fn + click** |
| Side-light channels | **do not work** | work |
| Per-key colour painting | works | works |

So: **flashing the stock image costs you the M10 key and the side-light
channels.** If you only want to undo something, restore the stock firmware and
accept that; if you want the side lights, stay on v1.0 or put it back afterwards.

Other things worth knowing before you start:

- **The app never writes bytes it cannot verify.** Two vendor actions —
  "type a text string" and "launch an application" — are **not offered**, because
  nobody has worked out how the firmware stores them. The buttons are absent
  rather than greyed out, so there is nothing to click and get a wrong answer.
- **A macro can be uploaded that the keyboard will not replay correctly.** If one
  misbehaves, use **Erase all macros** on the Macros tab, then unbind the key —
  see §6.
- **The keyboard cannot be read back.** This board answers read-back with zeros,
  so the app cannot tell you which macros are stored, and cannot verify a write
  after the fact. Macro uploads replace everything that was there.
- **An upload only sticks when the keyboard accepts the checksum.** It usually
  takes several attempts; the app retries automatically, and shows a ` *` on
  *Apply this layer* while a change has not been accepted.
- **The Fn layer is less well proven than the Top layer.** The upload is
  known-good in shape and matches the vendor's own sequence, but a commit made
  from this app has never been confirmed to survive a power cycle. The app says
  so on the tab.
- **No accented characters.** The HID keyboard page has no key code for
  `ä ö ü ß é è ç à ù`. The app will not fake them with a neighbouring key; set
  your operating system's language layout instead.

---

## 3. Installing

### What you need

| | |
|---|---|
| **Python** | 3.8 or newer, with pip |
| **PyQt6** | the GUI |
| **hidapi** | the USB transport (`libhidapi-hidraw` on Linux) |
| **python-evdev** | **only for recording macros** — everything else works without it |

Check Python:

```bash
python3 --version
```

### Linux (Debian / Ubuntu)

```bash
sudo apt install python3-pyqt6 libhidapi-hidraw0 python3-evdev
```

### Anything else

```bash
pip install PyQt6 evdev
```

and install hidapi from your package manager (`libhidapi` on Homebrew,
`hidapi` from MSYS2 on Windows). On Linux, `python3 -c "import ctypes.util;
print(ctypes.util.find_library('hidapi-hidraw'))"` should print something.

### Permissions

Two separate things need permission, and they are not the same.

**To read keystrokes for macro recording**, your user needs to be in the
`input` group. Without it the Record button is disabled and says so:

```bash
sudo usermod -aG input "$USER"
```

Log out and back in for this to take effect.

**To flash firmware without `sudo`**, add a udev rule:

```bash
sudo tee /etc/udev/rules.d/52-cloudnine.rules >/dev/null <<'EOF'
KERNEL=="hidraw*", ATTRS{idVendor}=="0c45", MODE="0660", TAG+="uaccess"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger
```

This matches the vendor ID alone, so it covers the keyboard in normal mode
(`0c45:8006`) and in bootloader mode (`0c45:7040`).

### Check it

```bash
lsusb | grep -i 0c45
```

You should see `ID 0c45:8006`. If the keyboard does not appear at all, see §6.

---

## 4. Running it

From the top of this folder:

```bash
python3 application/cloudnine_gui.py
```

or from inside `application/`:

```bash
python3 cloudnine_gui.py
```

Both work. If you hit an error about a missing module, §3 was not completed.

---

## 5. Restoring the stock (OEM) firmware

**Do this if you want the keyboard back exactly as it shipped**, or if a
modified image is misbehaving in a way you cannot explain. A known-good
baseline is more informative than another experiment on a suspect image.

Expect the two differences from §2 afterwards: the **M10/Light key will be
swallowed into the colour cycle again**, and the **side-light channels will
stop working**.

### From the app

**Firmware** tab → **Restore OEM firmware (original, unmodified)** → confirm.
It asks first, flashes, and reboots the keyboard.

### From the command line

```bash
cd firmware
./flash.sh --oem
```

A healthy run ends with:

```
Flash Verification Checksum: OK!
Device succesfully flashed!
```

(the typo is in the flasher's own output). The keyboard reboots back to normal
mode on its own; unplug and replug it if it does not.

### Directly, if the script is in the way

```bash
cd firmware
./sonixflasher --vidpid 0c45/8006 -r hfd -f firmware.oem.bin --offset 0x0
```

`0c45:8006` is the keyboard's **normal** USB ID; the flasher reboots it into the
bootloader itself using the vendor's HFD magic. Only use `0c45:7040` if the
board is *already* stuck in bootloader mode and cannot be rebooted that way.

Both images are 65536 bytes and flash at offset `0x0000`. The other option,
`--v1`, puts `firmware.v1.0.bin` back.

---

## 6. Hard reset

Work down this list; stop as soon as it comes back.

### A key is stuck, or a macro will not stop repeating

1. **Unplug the keyboard.** That is the reset. A macro that loops forever is
   firmware state and nothing in the app can halt it mid-playback; pulling the
   cable is what clears it.
2. Replug it. The macro is still stored, so do **not** press that key again yet.
3. On the **Macros** tab press **Erase all macros from the keyboard**. That
   uploads an empty macro region, so no key has anything to play. Your saved
   library in the app is not touched.
4. On the **Key remap** tab, select the key and press **Clear this key's
   mapping**, then **Apply this layer**.

### Nothing happens when you press keys

1. `lsusb | grep -i 0c45`
2. If you see **`0c45:8006`**, the keyboard is fine and this is a key mapping —
   press **Clear this key's mapping** for it.
3. If you see **`0c45:7040`**, it is in bootloader mode and just needs
   reflashing: §5.
4. If you see **no `0c45` device at all** — not even after a replug — go to the
   next section.

### The board is dead to the host (no `0c45` at all)

The keyboard's internal USB hub enumerates but the microcontroller never
appears. The board is powered and the data lines work; it is the MCU that is
gone, and **the normal flash path cannot help**, because flashing reboots the
*running* firmware into the bootloader and there is no running firmware. You have
to force the bootloader by hand, once.

There are two holes on the **underside of the board**, labelled `BOOT` and `R1`.

1. **Unplug** the keyboard.
2. **Remove the screws** from the back and open the case.
3. Find the two holes labelled **`BOOT`** and **`R1`** on the underside.
4. **Fit a jumper** across them.
5. **Power on** the keyboard with the jumper still in place.
6. **Remove the jumper** once it is powered. The chip has already sampled the
   pin and stays in the bootloader.
7. **Confirm it worked before you flash anything** — this is the step people
   skip:

   ```bash
   lsusb | grep -i 0c45      # must show 0c45:7040
   ```

   If it does not, power down and go back to step 4. Do not flash until the
   bootloader ID is actually there.
8. **Flash** (§5). The flasher drives the reboot from here; the jumper is no
   longer needed.
9. `lsusb | grep -i 0c45` should show `0c45:8006` again within a few seconds.
10. Close the case.

Pulsing reset on its own will not do this: a chip held in reset cannot answer a
reset request, and reset cannot fix whatever it would have booted into. Forcing
the bootloader at power-on bypasses that.

---

## 7. If something is still wrong

- Nothing happens when you press **Apply**. Watch the status line at the bottom
  — it says whether the change was accepted. A ` *` on the button means it is
  still unsent; press Apply again.
- The **Record macro** button is greyed out. That is the `input` group
  permission from §3, or `python-evdev` is not installed.
- **No such file** for the firmware. You are missing the `firmware/` folder next
  to `application/`.
- `libhidapi-hidraw.so.0: cannot open shared object file`. Install hidapi (§3).