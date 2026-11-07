#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-2-Clause-Patent
# Build only the native arm64e kernel, with caller-owned toolchain/firmware inputs.
set -euo pipefail
: "${HOUJI_XNU:?Path to the prepared iOS26 HOUJI kernel}"
: "${HOUJI_REFERENCE:?Pinned hakios checkout with the reference toolchain patch applied}"
task_root=$(cd "$(dirname "$0")/.." && pwd)
if [[ ! -f "$HOUJI_XNU/pexpert/pexpert/arm64/HOUJI.h" ||
      ! -f "$HOUJI_XNU/bsd/sweet_hfs/core/hfs_vfsops.c" ]]; then
  echo "Prepare the iOS26 kernel and pinned HFS sources first" >&2
  exit 1
fi
python3 "$task_root/tools/check-trial-bindings.py" "$HOUJI_XNU"
# The HID usage table is a local SDK input shared with the iOS16 build.
hid_header="$HOUJI_XNU/iokit/Kernel/arm/hid/IOHIDUsageTables.h"
if [[ ! -f "$hid_header" ]]; then
  sdk_root=$(xcrun --sdk macosx --show-sdk-path)
  sdk_hid=${HOUJI_HID_USAGE_HEADER:-"$sdk_root/System/Library/Frameworks/Kernel.framework/Headers/IOKit/hid/IOHIDUsageTables.h"}
  if [[ ! -r "$sdk_hid" ]]; then
    echo "Missing IOHIDUsageTables.h; set HOUJI_HID_USAGE_HEADER to the local SDK header" >&2
    exit 1
  fi
  cp "$sdk_hid" "$hid_header"
fi
export XNU_SRC="$HOUJI_XNU" XNU_ARCH=arm64e MACHINE=HOUJI
export RC_DARWIN_KERNEL_VERSION=25.6.0
bash "$HOUJI_REFERENCE/base/xnu-build.sh" -j"${HOUJI_JOBS:-8}" all HOUJI_STANDALONE_KERNEL=1
kernel="$HOUJI_XNU/BUILD/obj/mach.development.houji"
python3 - "$kernel" <<'PY'
import struct, subprocess, sys
from pathlib import Path
kernel = Path(sys.argv[1])
data = kernel.read_bytes()
magic, cpu, subtype = struct.unpack_from('<III', data)
assert magic == 0xfeedfacf and cpu == 0x100000c and subtype & 0xffffff == 2
assert b'Darwin Kernel Version 25.6.0:' in data
assert subprocess.check_output(['xcrun', 'nm', '-u', str(kernel)]).strip() == b''
print('PASS: linked arm64e HOUJI / Darwin 25.6.0 kernel; no unresolved symbols')
PY
python3 "$task_root/checks/ios26-pac.py" "$kernel"
echo "Standalone kernel built; SDK/kext packaging and hardware are outside this check"
