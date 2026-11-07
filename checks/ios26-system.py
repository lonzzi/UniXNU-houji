#!/usr/bin/env python3
"""Exercise the production full-system ADT builder and applied UEFI profile."""
import copy
import importlib.util
from pathlib import Path
import platform
import re
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
assert (ROOT / 'kernel/ios26/overlay/osfmk/arm64/HoujiPanic26.h').read_bytes() == (ROOT / 'uefi/ios26/overlay/Platforms/Xiaomi/houjiPkg/Application/HoujiStorageProbe/HoujiPanic26.h').read_bytes()
sys.path.insert(0, str(ROOT / 'tools'))
from ios26_adt import Node, parse, serialize

spec = importlib.util.spec_from_file_location('system_adt', ROOT / 'tools/make-ios26-system-adt.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
builder.unix_time = lambda: 1793923200


def node(name, properties=(), children=()):
    return Node([('name', 0, name.encode() + b'\0'), *[(k, 0, v) for k, v in properties]], list(children))


armio = node('arm-io', [('ranges', struct.pack('<QQQ', 0, 0x1000000, 0xff000000))], [
    node('gic', [('reg', struct.pack('<QQQQ', 0x16100000, 0x10000, 0x16180000, 0x200000)),
                 ('redistributor-stride', struct.pack('<Q', 0x40000))]),
    node('retained-device', [('sentinel', b'unchanged')]),
])
cpus = node('cpus', children=[node('cpu' + str(i), [
    ('reg', struct.pack('<I', i << 8)), ('cluster-id', struct.pack('<I', i % 2)),
    ('cluster-type', b'P\0'), ('cluster-core-id', struct.pack('<I', i)),
    ('small-core', bytes(4)), ('cpu-sentinel', b'retained'),
]) for i in range(8)])
chosen = node('chosen', [('houji,gpu-power-handoff', bytes(1376)),
                        ('houji,gpu-zap-reservation', bytes(512)),
                        ('local-sentinel', b'retained')], [node('memory-map', [
                            ('RAMDisk', bytes(16)), ('TrustCache', bytes(16)),
                            ('PackedRAMDisk', bytes(16)), ('retained-map', bytes(16)),
                        ])])
tree = node('device-tree', [('compatible', b'xiaomi,houji\0')], [armio, cpus, chosen])
donor = {'device-tree': {'model': 'iPhone18,1', 'target-type': 'V53',
                       'compatible': ['V53AP', 'iPhone18,1', 'AppleARM'],
                       'serial-number': 'must-never-be-copied'}}
raw = serialize(tree)
first, second = [parse(builder.prepare(raw, donor)) for _ in range(2)]
assert serialize(first.child('arm-io')) == serialize(armio)
assert first.get('model') == b'iPhone18,1\0' and first.get('serial-number') is None
assert first.child('chosen').get('local-sentinel') == b'retained'
assert first.child('chosen').get('darwinboot-memory-size') == struct.pack('<Q', 8 << 30)
assert first.child('chosen').get('boot-epoch') == struct.pack('<Q', 1793923200)
for name, length in [('random-seed', 256), ('houji,pac-entropy', 80)]:
    a, b = first.child('chosen').get(name), second.child('chosen').get(name)
    assert len(a) == len(b) == length and a != b
for i, cpu in enumerate(first.child('cpus').children):
    assert cpu.get('reg') == struct.pack('<I', i << 8)
    assert cpu.get('state') == (b'running\0' if i == 0 else b'waiting\0')
    assert cpu.get('cpu-sentinel') == b'retained'
    assert all(cpu.get(k) is None for k in ('cluster-id', 'cluster-type', 'cluster-core-id', 'small-core'))
memory = first.child('chosen').child('memory-map')
assert all(memory.get(k) is None for k in ('RAMDisk', 'PackedRAMDisk', 'TrustCache'))
assert memory.get('retained-map') == bytes(16)


def rejected(changed, identity=donor):
    try:
        builder.prepare(serialize(changed), identity)
    except ValueError:
        return
    raise AssertionError('Unsafe full-system handoff accepted')


bad = copy.deepcopy(tree);bad.child('cpus').children.pop();rejected(bad)
bad = copy.deepcopy(tree);bad.child('cpus').child('cpu7').set('reg', bytes(4));rejected(bad)
bad = copy.deepcopy(tree);bad.child('arm-io').child('gic').set('redistributor-stride', bytes(8));rejected(bad)
bad = copy.deepcopy(tree);bad.child('chosen').set('houji,gpu-zap-reservation', b'x' * 512);rejected(bad)
bad = copy.deepcopy(tree);bad.child('chosen').set('houji,gpu-power-handoff', bytes(1375));rejected(bad)
rejected(tree, {'device-tree': {**donor['device-tree'], 'model': 'iPhone18,2'}})

with tempfile.TemporaryDirectory(prefix='houji26-system-') as temporary:
    temporary = Path(temporary)
    adt = temporary / 'system.adt';adt.write_bytes(serialize(first))
    subprocess.run([sys.executable, str(ROOT / 'checks/ios26-entry.py'), '--adt', str(adt)], check=True)
    relative = Path('Platforms/Xiaomi/houjiPkg/Application/HoujiStorageProbe/HoujiStorageProbe.c')
    source = temporary / relative;source.parent.mkdir(parents=True)
    source.write_bytes((ROOT / 'uefi/overlay' / relative).read_bytes())
    guard = relative.with_name('UfsLogWriteGuard.h')
    (temporary / guard).write_bytes((ROOT / 'uefi/overlay' / guard).read_bytes())
    subprocess.run(['git', 'apply', str(ROOT / 'uefi/ios26/patches/local.patch')], cwd=temporary, check=True)
    content = source.read_text()
    args = re.search(r'CONST CHAR16 \*BaseBootArgs = L"([^"\n]+)";', content)[1]
    tokens = args.split();names = [v.split('=', 1)[0] for v in tokens]
    assert len(set(names)) == len(names) and len(args + ' houji_touch=1') < 1024
    assert 'CHAR16 BootArgs[1024]' in content and 'StrLen(L" houji_touch=1")' in content
    assert all(v in tokens for v in ('cpus=8', 'serial=0', 'houji_native_root=1', 'houji_ufs_log=1',
                                   'houji_return_recovery=1', 'houji_trial_seconds=300',
                                   'houji_gpu_runtime=5', 'houji_dvfs=3', 'gpu_max_mhz=903',
                                   'houji_native_hid=1', 'gtrace=0', 'sample=0',
                                   'sweetsample=-', 'sweetiolog=-'))
    assert 'mTouchHandoffReady?1:0' in content
    assert content.index('Status = RunLogFsTest (ImageHandle);') < content.index('Houji26StartupFailureReset (Status, StartupRecoveryReady)') < content.index('Status=GpuPowerHandoffPrepare(ImageHandle);')
    executable = temporary / 'startup-recovery'
    subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                    '-I', str(ROOT / 'uefi/ios26/overlay' / relative.parent),
                    '-I', str(ROOT / 'uefi/overlay' / relative.parent),
                    str(ROOT / 'checks/ios26-startup.c'), '-o', str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
    flush = temporary / 'flush'
    subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                    '-I', str(source.parent), str(ROOT / 'checks/ios26-flush.c'), '-o', str(flush)], check=True)
    subprocess.run([str(flush)], check=True)
    includes = ['-I', str(ROOT / 'checks/ios26-uefi'), '-I', str(ROOT / 'bootloader/ios26/overlay/DarwinBootPkg/Driver')]
    offsets = temporary / 'offsets'
    subprocess.run(['clang', '-std=c11', '-DPRINT_OFFSETS', *includes,
                    str(ROOT / 'checks/ios26-screen.c'), '-o', str(offsets)], check=True)
    (temporary / 'offsets.h').write_bytes(subprocess.check_output([str(offsets)]))
    assembly = temporary / 'screen.S'
    assembly.write_text('#define HOUJI_IOS26 1\n#include "offsets.h"\n#include "HoujiEarly26.inc"\n'
                        '.text\n.globl _test_screen\n.p2align 2\n_test_screen:\n'
                        'stp x20,x30,[sp,#-16]!\nmov x20,x0\nHOUJI26_SCREEN 0x00ff,0xffff\n'
                        'ldp x20,x30,[sp],#16\nret\n')
    screen_includes = [*includes, '-I', str(ROOT / 'kernel/ios26/overlay/osfmk/arm64')]
    if platform.system() == 'Darwin':
        screen = temporary / 'screen'
        subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                        '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                        *screen_includes, str(assembly), str(ROOT / 'checks/ios26-screen.c'),
                        '-o', str(screen)], check=True)
        subprocess.run([str(screen)], check=True)
        print('PASS: actual AArch64 screen marker checks pixel bounds/invalid geometry; no MMU emulation')
    else:
        # The production XNU macro uses the Darwin assembler's argument syntax
        # and Mach-O symbols. Keep that source intact when checking it on Linux.
        subprocess.run(['clang', '-target', 'arm64-apple-macos11', *screen_includes,
                        '-c', str(assembly), '-o', str(temporary / 'screen.o')], check=True)
        print('PASS: production AArch64 screen marker cross-assembles for Darwin; execution requires macOS')
    reserved = temporary / 'reserved-nc'
    subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                    '-I', str(ROOT / 'kernel/ios26/overlay/osfmk/arm64'),
                    str(ROOT / 'checks/ios26-reserved-nc.c'), '-o', str(reserved)], check=True)
    subprocess.run([str(reserved)], check=True)
    terminal = temporary / 'terminal'
    subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                    '-I', str(ROOT / 'kernel/ios26/overlay/osfmk/arm64'),
                    str(ROOT / 'checks/ios26-terminal.c'), '-o', str(terminal)], check=True)
    subprocess.run([str(terminal)], check=True)
    rootfs = temporary / 'rootfs'
    subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                    '-I', str(ROOT / 'kernel/ios26/overlay/bsd/sys'),
                    str(ROOT / 'checks/ios26-rootfs.c'), '-o', str(rootfs)], check=True)
    subprocess.run([str(rootfs)], check=True)
    gfx = Path('iokit/Kernel/houji-ufs-root/HoujiGpuNativeGfx.inc')
    target = temporary / gfx;target.parent.mkdir(parents=True)
    target.write_bytes((ROOT / 'kernel/overlay' / gfx).read_bytes())
    subprocess.run(['git', 'apply', '--include=' + str(gfx),
                    str(ROOT / 'kernel/ios26/patches/reused-drivers.patch')], cwd=temporary, check=True)
    content = target.read_text()
    assert content.index('hzr_valid(&g.handoff') < content.index('houji26_register_reserved_nc') < content.index('IODeviceMemory::withRange')
    assert '!hzr_par(r,g.pa[which]+i*4096)' in content
print('PASS: production full-system ADT preserves houji hardware, validates CPU/GPU slots and fresh entropy; applied UEFI uses Darwin 25 bounds and mandatory startup gates; no hardware emulation')
print('PASS: production early-failure recovery requires exact live BCB readback, preserves status, waits ten seconds, retains handlers and performs no writes; host callbacks only')
print('PASS: exact no-payload LUN0 flush survives a closed data-write gate; WRITE10 still requires arming and its bounded range; no firmware emulation')
print('PASS: production reserved-NC policy publishes only two validated 16K pages, excludes padding/PIL neighbours/kernel RAM, rejects repeat registration and wrap, and retains HZR/AT checks; no GPU qualification')
print('PASS: terminal renderer respects mapping capacity, visible pixels, stride padding, text bounds and invalid geometry; no panic-path I/O or display qualification')
print('PASS: bounded first-panic RAM record rejects a different trial, corruption and invalid owned/system RAM ranges; kernel/firmware ABI matches; cross-reset retention unqualified')
print('PASS: native HFS root selection skips an earlier always-successful mockfs, fails closed on HFS error and preserves the minimum RAM probe; host callback outcomes, no filesystem mount claim')
