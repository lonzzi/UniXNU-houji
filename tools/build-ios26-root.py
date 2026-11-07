#!/usr/bin/env python3
"""Assemble an isolated iOS26 HFSX image on macOS; never opens a phone device."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SIZE = 32 * 1024 ** 3
SHARED_CACHE_ALIAS = '../../Cryptexes/OS/System/Library/Caches/com.apple.dyld'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run(*command):
    return subprocess.check_output(list(map(str, command)))


def mounted_image(path, writable):
    info = plistlib.loads(run('diskutil', 'info', '-plist', path))
    if (info.get('BusProtocol') != 'Disk Image' or
        info.get('MountPoint') != str(path) or info.get('Writable') is not writable):
        raise ValueError('Expected a local disk-image mount with the requested access: ' + str(path))
    return info


def ordinary_file(path):
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('Expected an ordinary local file: ' + str(path))


def build_files(directory):
    report = json.loads((directory / 'build-result.json').read_text())
    if report['architecture'] != 'arm64e':
        raise ValueError('All userland components must be arm64e')
    if 'files' in report:
        for name, expected in report['files'].items():
            rel = Path(name)
            if rel.is_absolute() or '..' in rel.parts or digest(directory / rel) != expected:
                raise ValueError('Userland helper input changed')
    return report


def directory_in_root(root, relative):
    current = root
    for part in Path(relative).parts:
        if part in ('..', '/'):
            raise ValueError('Invalid image-relative directory')
        current /= part
        if current.is_symlink():
            raise ValueError('Refusing to write through an image symlink: ' + relative)
        current.mkdir(mode=0o755, exist_ok=True)
    return current


def replace_directory(root, relative):
    rel = Path(relative)
    parent = directory_in_root(root, str(rel.parent))
    target = parent / rel.name
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    target.mkdir(mode=0o755)
    return target


def install_shared_cache_view(root):
    # This flattened HFS root has no APFS graft/ignition discovery. Expose the
    # unchanged Cryptex cache at dyld's ordinary iPhone fallback directory too.
    alias = directory_in_root(root, 'System/Library/Caches') / 'com.apple.dyld'
    if alias.is_symlink() and os.readlink(alias) == SHARED_CACHE_ALIAS:
        return
    if alias.is_symlink() or alias.exists():
        raise ValueError('Refusing to replace an occupied system shared-cache path')
    alias.symlink_to(SHARED_CACHE_ALIAS, target_is_directory=True)


def verify_cryptex_views(root):
    root = root.resolve()
    for name in ('OS', 'App'):
        relative = Path('private/preboot/Cryptexes') / name
        canonical = root / relative
        alias = root / 'System/Cryptexes' / name
        expected = '../../private/preboot/Cryptexes/' + name
        if (canonical.is_symlink() or not canonical.is_dir() or
                canonical.resolve() != root / relative or not alias.is_symlink() or
                os.readlink(alias) != expected or alias.resolve() != canonical):
            raise ValueError('Cryptex canonical path or stock alias is invalid: ' + name)
    cache = root / 'private/preboot/Cryptexes/OS/System/Library/Caches/com.apple.dyld'
    fallback = root / 'System/Library/Caches/com.apple.dyld'
    if (not fallback.is_symlink() or os.readlink(fallback) != SHARED_CACHE_ALIAS or
            fallback.resolve() != cache or cache.resolve() != cache):
        raise ValueError('System shared-cache fallback must resolve to the canonical Cryptex cache')
    ordinary_file(cache / 'dyld_shared_cache_arm64e')
    with (cache / 'dyld_shared_cache_arm64e').open('rb') as stream:
        magic = stream.read(16)
    if not magic.startswith(b'dyld_v1') or b'arm64e' not in magic:
        raise ValueError('Expected the arm64e shared cache at its canonical preboot path')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline', type=Path, required=True, help='Verified immutable 32 GiB Recovery snapshot')
    p.add_argument('--system-root', type=Path, required=True)
    p.add_argument('--cryptex-os', type=Path, required=True)
    p.add_argument('--cryptex-app', type=Path, required=True)
    p.add_argument('--exclave', type=Path, required=True)
    p.add_argument('--userland-build', type=Path, required=True)
    p.add_argument('--metal-build', type=Path, required=True)
    p.add_argument('--apps-build', type=Path, required=True)
    p.add_argument('--compat-build', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True, help='Must not exist')
    p.add_argument('--plan', action='store_true', help='Validate inputs and print the exact local plan without writes')
    a = p.parse_args()
    if sys.platform != 'darwin':
        p.error('macOS is required for local HFSX authoring')
    for key, value in vars(a).items():
        if isinstance(value, Path):
            if key == 'baseline':
                ordinary_file(value)
            setattr(a, key, value.resolve())
    if a.output.exists() or a.output == Path('/') or not a.output.parent.is_dir():
        p.error('Use a new output directory under an existing local artifact directory')
    if a.baseline.stat().st_size != SIZE:
        p.error('Expected the qualified 32 GiB snapshot geometry')
    before = json.loads((a.baseline.parent / 'backup.json').read_text())
    if (before['bytes'] != SIZE or not before.get('phone_chunk_index_stable') or
        not before.get('every_output_chunk_matches_device') or digest(a.baseline) != before['sha256']):
        p.error('A complete stable Recovery snapshot with verified content is required')
    with a.baseline.open('rb') as stream:
        header = stream.read(4096)
    if (header[1024:1028] != b'HX\x00\x05' or header[1064:1072] != bytes.fromhex('0000100000800000') or
        header[1128:1136] != bytes.fromhex('1e38300864ba2b7b')):
        p.error('Unqualified HFSX geometry or volume identity')
    for source in [a.system_root, a.cryptex_os, a.cryptex_app, a.exclave]:
        if mounted_image(source, False).get('GlobalPermissionsEnabled') is not True:
            p.error('Enable numeric ownership on the read-only donor mount: ' + str(source))
    version = plistlib.loads((a.system_root / 'System/Library/CoreServices/SystemVersion.plist').read_bytes())
    if (version['ProductVersion'], version['ProductBuildVersion']) != ('26.6.2', '23G90'):
        p.error('Unexpected donor system')
    userland = build_files(a.userland_build)
    if userland.get('data_numeric_owners_verified') is not True:
        p.error('Rebuild the Data template from the donor with numeric ownership enabled')
    metal, apps, compat = [build_files(d) for d in [a.metal_build, a.apps_build, a.compat_build]]
    if (metal.get('runtime_logging') is not False or
        metal.get('deployment_target') != '26.0' or apps.get('profile') != 'ios26' or
        compat.get('profile') != 'ios26' or userland.get('donor_build') != '23G90' or
        digest(a.metal_build / 'NativeAdrenoMetal.bundle/NativeAdrenoMetal') != metal['plugin_sha256'] or
        digest(a.apps_build / 'houji-system-apps') != apps['binary_sha256'] or
        digest(a.compat_build / 'HoujiKeyboardGeometry.dylib') != compat['library_sha256']):
        p.error('A component changed or runtime GPU logging is enabled')
    plan = {'operation': 'author a new local image only', 'output': str(a.output),
            'baseline_sha256': before['sha256'], 'bytes': SIZE, 'donor_build': '23G90',
            'source_mounts_readonly': True, 'numeric_ownership': 'preserve donor; root for own code and jobs',
            'contents': ['full stock OS and staged system apps', 'OS and App cryptexes with all dyld subcaches',
                         'ExclaveOS assets', 'version-matched arm64e init and launchd injection',
                         'arm64e Metal/bridge/Turnip', 'keyboard/status-bar compatibility', 'system-app registrar'],
            'data_mode': userland['data_mode'], 'phone_accessed': False, 'partitions_written': False}
    if a.plan:
        print(json.dumps(plan, indent=2));return
    if os.geteuid() != 0:
        p.error('Local image authoring requires macOS administrator authentication to preserve numeric iOS owners')
    owner = a.output.parent.stat().st_uid
    a.output.mkdir(mode=0o700)
    image, mount = a.output / 'ios26-root.hfsx', a.output / 'mount'
    mount.mkdir()
    subprocess.run(['cp', '-c', str(a.baseline), str(image)], check=True)
    image.chmod(0o600)
    if image.samefile(a.baseline):
        raise ValueError('Clone aliases the immutable snapshot')
    attachment = plistlib.loads(run('hdiutil', 'attach', '-imagekey', 'diskimage-class=CRawDiskImage',
                                  '-owners', 'on', '-nobrowse',
                                  '-mountpoint', mount, '-plist', image))
    devices = [d['dev-entry'] for d in attachment['system-entities']]
    device = min(devices, key=len)
    if [d.get('mount-point') for d in attachment['system-entities'] if 'mount-point' in d] != [str(mount)]:
        raise ValueError('Unexpected image attachment')
    try:
        info = mounted_image(mount, True)
        if info.get('GlobalPermissionsEnabled') is not True:
            raise ValueError('Numeric ownership is not enabled on the image')
        # Only this newly created, verified local image is cleared. HFS journal
        # metadata belongs to the filesystem and is retained.
        for child in mount.iterdir():
            if child.name in ('.journal', '.journal_info_block') or 'HFS+ Private' in child.name:
                continue
            if child.is_symlink() or not child.is_dir():
                child.unlink()
            else:
                shutil.rmtree(child)
        def copy_tree(source, destination, *options):
            subprocess.run(['/usr/bin/rsync', '-aH', '--numeric-ids', *options,
                            str(source) + '/', str(destination) + '/'], check=True)
        print('Copying full donor OS into isolated HFSX image', flush=True)
        copy_tree(a.system_root, mount, '--exclude=/private/var/staged_system_apps')
        for source, relative in [(a.cryptex_os, 'private/preboot/Cryptexes/OS'),
                                 (a.cryptex_app, 'private/preboot/Cryptexes/App'),
                                 (a.exclave, 'private/preboot/Cryptexes/ExclaveOS')]:
            destination = replace_directory(mount, relative)
            copy_tree(source, destination)
        install_shared_cache_view(mount)
        verify_cryptex_views(mount)
        applications = directory_in_root(mount, 'Applications')
        for app in (a.system_root / 'private/var/staged_system_apps').iterdir():
            target = applications / app.name
            if target.exists():
                raise ValueError('Duplicate staged system app: ' + app.name)
            if app.is_symlink() or not app.is_dir():
                raise ValueError('Unexpected staged application')
            target.mkdir()
            copy_tree(app, target)
            os.chown(target, app.stat().st_uid, app.stat().st_gid)
            target.chmod(stat.S_IMODE(app.stat().st_mode))
        helper = replace_directory(mount, 'redwood')
        copy_tree(a.userland_build / 'redwood', helper)
        for parent, directories, files in os.walk(helper, followlinks=False):
            os.chown(parent, 0, 0)
            for name in directories + files:
                os.lchown(Path(parent) / name, 0, 0)
        compat_target = helper / 'lib/HoujiKeyboardGeometry.dylib'
        shutil.copy2(a.compat_build / compat_target.name, compat_target)
        os.chown(compat_target, 0, 0)
        original_launchd = directory_in_root(mount, 'sbin') / 'launchd'
        original_launchd.rename(mount / 'sbin/launchd.ios')
        shutil.copyfile(a.userland_build / 'launchd', original_launchd)
        original_launchd.chmod(0o755);os.chown(original_launchd, 0, 0)
        extensions = directory_in_root(mount, 'System/Library/Extensions')
        bundle = extensions / 'NativeAdrenoMetal.bundle'
        shutil.copytree(a.metal_build / bundle.name, bundle)
        for parent, directories, files in os.walk(bundle):
            os.chown(parent, 0, 0)
            for name in directories + files:
                os.chown(Path(parent) / name, 0, 0)
        shutil.copyfile(a.metal_build / 'NativeAdrenoMetal.clients', extensions / 'NativeAdrenoMetal.clients')
        jobs = replace_directory(mount, 'System/Library/LaunchDaemons')
        for relative in ['System/Library/NanoLaunchDaemons', 'System/Library/LaunchAngels']:
            replace_directory(mount, relative)
        cache = directory_in_root(mount, 'System/Library/xpc') / 'launchd.plist'
        if cache.is_symlink() or cache.exists():
            cache.unlink()
        listing = a.output / 'system-paths.txt'
        with listing.open('w') as stream:
            for parent, directories, files in os.walk(a.system_root, followlinks=False):
                for name in directories + files:
                    stream.write('Path = ' + str((Path(parent) / name).relative_to(a.system_root)) + '\n')
        view = a.output / 'source-view'
        view.mkdir()
        for name, source in [('rootfs', a.system_root), ('cryptex', a.cryptex_os), ('cryptex-app', a.cryptex_app)]:
            (view / name).symlink_to(source, target_is_directory=True)
        policy = json.loads((ROOT / 'userland/ios26/launchd.json').read_text())
        available = a.system_root / 'System/Library/LaunchDaemons'
        labels = [label for label in policy['run_at_boot'] if (available / (label + '.plist')).is_file()]
        if not set(policy['required']).issubset(labels):
            raise ValueError('Missing required launchd jobs')
        env = dict(os.environ, OSLOG='', OSLOG_LEVEL='', ALLOW='', RWA_PACKED16='')
        subprocess.run([sys.executable, str(a.userland_build / 'source/mkjobs.py'), '--rest',
                        '--root-volume', str(listing), str(view / 'rootfs'), str(jobs), *labels], env=env, check=True)
        for plist in jobs.glob('*.plist'):
            job = plistlib.loads(plist.read_bytes())
            job['StandardOutPath'] = job['StandardErrorPath'] = '/dev/null'
            for key in ['OS_ACTIVITY_DT_MODE', 'ACTIVITY_LOG_STDERR', 'CFLOG_FORCE_STDERR', 'OS_ACTIVITY_MODE']:
                job.get('EnvironmentVariables', {}).pop(key, None)
            plist.write_bytes(plistlib.dumps(job, fmt=plistlib.FMT_BINARY))
            plist.chmod(0o644);os.chown(plist, 0, 0)
        app_program = directory_in_root(mount, 'usr/libexec') / 'houji-system-apps'
        shutil.copyfile(a.apps_build / app_program.name, app_program)
        app_program.chmod(0o755);os.chown(app_program, 0, 0)
        app_job = jobs / 'moe.houji.system-apps.plist'
        shutil.copyfile(ROOT / 'userland/apps/moe.houji.system-apps.plist', app_job)
        app_job.chmod(0o644);os.chown(app_job, 0, 0)
        # Verify all donor copies byte-for-byte plus numeric modes/owners. The
        # explicitly replaced paths are checked independently below.
        verified = 0
        skip = ('sbin/launchd', 'System/Library/LaunchDaemons', 'System/Library/NanoLaunchDaemons',
                'System/Library/LaunchAngels', 'System/Library/xpc/launchd.plist', 'private/var/staged_system_apps',
                'private/preboot/Cryptexes/OS', 'private/preboot/Cryptexes/App', 'private/preboot/Cryptexes/ExclaveOS')
        for source, target, exclusions in [(a.system_root, mount, skip),
                (a.cryptex_os, mount / 'private/preboot/Cryptexes/OS', ()),
                (a.cryptex_app, mount / 'private/preboot/Cryptexes/App', ()),
                (a.exclave, mount / 'private/preboot/Cryptexes/ExclaveOS', ())]:
            for parent, directories, files in os.walk(source, followlinks=False):
                relparent = Path(parent).relative_to(source)
                for name in directories + files:
                    src = Path(parent) / name;rel = relparent / name
                    if any(str(rel) == e or str(rel).startswith(e + '/') for e in exclusions):
                        continue
                    dst = target / rel
                    left, right = src.lstat(), dst.lstat()
                    if (stat.S_IFMT(left.st_mode), stat.S_IMODE(left.st_mode), left.st_uid, left.st_gid) != (
                        stat.S_IFMT(right.st_mode), stat.S_IMODE(right.st_mode), right.st_uid, right.st_gid):
                        raise ValueError('Copied metadata differs: ' + str(rel))
                    if stat.S_ISLNK(left.st_mode) and os.readlink(src) != os.readlink(dst):
                        raise ValueError('Copied symlink differs: ' + str(rel))
                    if stat.S_ISREG(left.st_mode) and digest(src) != digest(dst):
                        raise ValueError('Copied bytes differ: ' + str(rel))
                    verified += 1
            print('Verified donor copy:', source.name, verified, 'entries', flush=True)
        if digest(mount / 'sbin/launchd.ios') != digest(a.system_root / 'sbin/launchd'):
            raise ValueError('Original launchd changed')
        for relative, expected in userland['files'].items():
            target = mount / ('sbin/launchd' if relative == 'launchd' else relative)
            if digest(target) != expected:
                raise ValueError('Installed helper differs: ' + relative)
        if digest(bundle / 'NativeAdrenoMetal') != metal['plugin_sha256']:
            raise ValueError('Installed Metal frontend differs')
        free = os.statvfs(mount).f_bavail * os.statvfs(mount).f_frsize
        (a.output / 'filesystem-check.log').write_bytes(run('diskutil', 'verifyVolume', mount))
        plan.update(verified_donor_entries=verified, free_bytes=free, launchd_jobs=len(list(jobs.glob('*.plist'))),
                    original_launchd_preserved=True, runtime_collectors=False, filesystem_check='PASS')
    finally:
        run('hdiutil', 'detach', device)
    with image.open('rb') as stream:
        after = stream.read(4096)
    if any(after[start:start+length] != header[start:start+length] for start, length in [(1024,4),(1064,8),(1128,8)]):
        raise ValueError('HFSX identity changed')
    plan.update(image_sha256=digest(image), hardware_verified=False, installed=False)
    (a.output / 'build-result.json').write_text(json.dumps(plan, indent=2) + '\n')
    image.chmod(0o400)
    for path in [a.output, *a.output.rglob('*')]:
        os.lchown(path, owner, -1)
    print('Built and verified isolated iOS26 HFSX image; no phone partitions accessed')


if __name__ == '__main__':
    main()
