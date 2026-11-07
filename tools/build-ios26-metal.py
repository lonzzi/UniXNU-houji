#!/usr/bin/env python3
"""Link the iOS26 arm64e Metal frontend with the matching PAC bridge and Turnip."""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import re
import shutil
import struct
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def arm64e(path):
    magic, cpu, subtype = struct.unpack_from('<III', path.read_bytes())
    return (magic, cpu, subtype & 0xffffff) == (0xfeedfacf, 0x100000c, 2)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bridge-build', type=Path, required=True)
    p.add_argument('--turnip-build', type=Path, required=True)
    p.add_argument('--mesa-source', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True, help='Must not exist')
    args = p.parse_args()
    lock = json.loads((ROOT / 'sources.lock.json').read_text())
    bridge_dir, turnip_dir, mesa = args.bridge_build.resolve(), args.turnip_build.resolve(), args.mesa_source.resolve()
    bridge = json.loads((bridge_dir / 'build-result.json').read_text())
    turnip = json.loads((turnip_dir / 'build-result.json').read_text())
    archive = bridge_dir / 'libnative_metal_bridge.a'
    dylib = turnip_dir / 'libvulkan_freedreno.dylib'
    if (bridge['architecture'] != 'arm64e' or not bridge['std_rebuilt'] or
        bridge['runtime_id'] != lock['ios26']['graphics_bridge']['runtime_id'] or
        bridge['target_sha256'] != lock['ios26']['rust']['target_sha256'] or
        digest(archive) != bridge['archive_sha256'] or
        not all(bridge['machine_code'].values())):
        p.error('A verified matching arm64e PAC bridge/std build is required')
    if (turnip['architecture'] != 'arm64e' or turnip['kernel_abi'] != 1 or
        turnip['source_tree_sha256'] != lock['ios26']['turnip']['source_tree_sha256'] or
        digest(dylib) != turnip['dylib_sha256'] or not arm64e(dylib)):
        p.error('A matching arm64e Turnip build is required')
    if json.loads((mesa / '.houji-source.json').read_text()) != {
        'component': 'graphics/turnip', 'revision': turnip['source_revision']
    }:
        p.error('Mesa headers differ from the prepared Turnip revision')
    out = args.output.resolve()
    if out.exists():
        p.error('Output exists')
    clients = (ROOT / 'graphics/metal/NativeAdrenoMetal.clients').read_text().splitlines()
    if not clients or len(set(clients)) != len(clients) or any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', c) for c in clients):
        p.error('Invalid explicit client allowlist')
    out.mkdir(parents=True)
    sdk = subprocess.check_output(['xcrun', '--sdk', 'iphoneos', '--show-sdk-path'], text=True).strip()
    common = ['-target', 'arm64e-apple-ios26.0', '-isysroot', sdk, '-O2',
              '-fstack-protector-strong', '-DHOUJI_TURNIP_RENDER_AUTOTUNE=0',
              '-DHOUJI_GPU_STAGE_TRACE=0', '-DHOUJI_METAL_QUIET=1',
              '-DHOUJI_GPU_CPU_PROFILE=0', '-DHOUJI_METAL_SINGLE_SAMPLE=1', '-DHOUJI_IOS26_METAL=1',
              '-DHOUJI_METAL_PIXEL_PROBE=0', '-I', str(ROOT / 'graphics/include'),
              '-I', str(mesa / 'include')]
    bundle = out / 'NativeAdrenoMetal.bundle'
    bundle.mkdir()
    pm4 = ['xcrun', 'clang', *common, '-std=c11', '-c', str(ROOT / 'graphics/include/AdrenoPM4.c'),
           '-o', str(out / 'AdrenoPM4.o')]
    objc = ['xcrun', 'clang++', *common, '-g', '-std=c++17', '-fobjc-arc',
            '-Wno-deprecated-declarations', '-bundle', '-x', 'objective-c++',
            str(ROOT / 'graphics/metal/AdrenoMetal/NativeAdrenoMetalPlugin.mm'),
            str(ROOT / 'graphics/metal/AdrenoMetal/NativeBridgeMetalFrontend.mm'), '-x', 'none',
            str(out / 'AdrenoPM4.o'), str(archive), '-Wl,-dead_strip', '-lobjc',
            '-framework', 'Foundation', '-framework', 'Metal', '-framework', 'IOSurface',
            '-o', str(bundle / 'NativeAdrenoMetal')]
    for name, command in [('pm4', pm4), ('frontend', objc)]:
        (out / (name + '-command.json')).write_text(json.dumps(command, indent=2) + '\n')
        with (out / (name + '-build.log')).open('w') as stream:
            subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=True)
    binary = bundle / 'NativeAdrenoMetal'
    if not arm64e(binary):
        raise ValueError('Metal frontend is not arm64e')
    info = dict(CFBundleExecutable='NativeAdrenoMetal', CFBundleIdentifier='org.blu-sharky.NativeAdrenoMetal',
                CFBundlePackageType='BNDL', CFBundleShortVersionString='1', CFBundleVersion='1',
                MinimumOSVersion='26.0', NSPrincipalClass='NativeAdrenoMetalDevice')
    (bundle / 'Info.plist').write_bytes(plistlib.dumps(info))
    shutil.copy2(dylib, bundle / dylib.name)
    shutil.copy2(binary, out / 'NativeAdrenoMetal.unstripped')
    symbols = bundle / 'NativeAdrenoMetal.dSYM'
    if symbols.exists():
        shutil.move(str(symbols), out / symbols.name)
    (out / 'NativeAdrenoMetal.clients').write_text('\n'.join(clients) + '\n')
    subprocess.run(['xcrun', 'strip', '-x', str(binary)], check=True)
    subprocess.run(['codesign', '--force', '--sign', '-', '--pagesize', '4096', '--timestamp=none', str(bundle)], check=True)
    subprocess.run(['codesign', '--verify', '--strict', str(bundle)], check=True)
    report = {'architecture': 'arm64e', 'deployment_target': '26.0', 'kernel_abi': 1,
              'plugin_sha256': digest(binary), 'bridge_archive_sha256': digest(archive),
              'runtime_id': bridge['runtime_id'], 'turnip_sha256': digest(bundle / dylib.name),
              'turnip_build_id': turnip['build_id'], 'authored_air_sha256': bridge.get('authored_air_sha256'),
              'render_policy': 'sysmem,nocb', 'single_sample': True,
              'runtime_logging': False, 'tracing': False, 'sampling': False,
              'hardware_verified': False, 'device_accessed': False, 'installed': False}
    (out / 'build-result.json').write_text(json.dumps(report, indent=2) + '\n')
    print('Built arm64e Metal frontend/bridge/Turnip bundle; no GPU or frame-rate qualification')


if __name__ == '__main__':
    main()
