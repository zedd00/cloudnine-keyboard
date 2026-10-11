#!/usr/bin/env bash
#
# Flash firmware to the Cloudnine ErgoFSK C989 (Sonix SN32F240B).
#
# Usage:
#   ./flash.sh                 # flash our latest (firmware.v1.0.bin)
#   ./flash.sh firmware.bin    # flash a specific image
#   ./flash.sh --v1            # flash firmware.v1.0.bin (ours)
#   ./flash.sh --oem           # flash firmware.oem.bin (untouched factory)
#
# Legacy aliases: --wheel-ring = --v1, --revert = --oem.
#
# History-only diagnostics (a repo checkout has these; a release does not):
#   ./flash.sh --light-remap     firmware.strip28m10.bin (Light key types J)
#   ./flash.sh --light-unassigned firmware.strip28m10_unassigned.bin (inert)
#
# This enters ISP mode via the HFD reboot magic (the only protocol this
# firmware supports), flashes at offset 0x0, verifies, and reboots.

set -euo pipefail

DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
FLASHER="$DIR/sonixflasher"
V1FW="$DIR/firmware.v1.0.bin"
OEMFW="$DIR/firmware.oem.bin"
LIGHTFW="$DIR/firmware.strip28m10.bin"
LIGHTUNASSIGNEDFW="$DIR/firmware.strip28m10_unassigned.bin"
VIDPID="0c45/8006"   # normal-mode USB ID of the keyboard
OFFSET="0x0"         # full image, no jumploader

usage() {
    # Print the comment block after the shebang, stripping the leading "# ".
    # Derived from the file rather than a hardcoded line range, so adding an
    # option to the header cannot silently truncate --help.
    awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
    exit 0
}

if [[ -z "${1:-}" ]]; then
    TARGET="$V1FW"
elif [[ "$1" == "--v1" || "$1" == "--wheel-ring" ]]; then
    TARGET="$V1FW"
elif [[ "$1" == "--oem" || "$1" == "--revert" ]]; then
    TARGET="$OEMFW"
elif [[ "$1" == "--light-remap" ]]; then
    TARGET="$LIGHTFW"
elif [[ "$1" == "--light-unassigned" ]]; then
    TARGET="$LIGHTUNASSIGNEDFW"
elif [[ "$1" == "--help" || "$1" == "-h" ]]; then
    usage
else
    TARGET="$1"
fi

if [[ ! -x "$FLASHER" ]]; then
    echo "ERROR: flasher not found at $FLASHER" >&2
    exit 1
fi
if [[ ! -f "$TARGET" ]]; then
    echo "ERROR: firmware file not found: $TARGET" >&2
    exit 1
fi

echo "==> Flashing $TARGET to $VIDPID at offset $OFFSET"

if ! lsusb 2>/dev/null | grep -qi "0c45"; then
    echo "!! Keyboard not found on USB. Is it plugged in?" >&2
    echo "!! Expected device with USB ID: $VIDPID" >&2
fi

"$FLASHER" --vidpid "$VIDPID" -r hfd -f "$TARGET" --offset "$OFFSET"

echo "==> Done. Keyboard should be back in normal mode."
