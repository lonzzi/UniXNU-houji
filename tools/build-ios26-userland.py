#!/usr/bin/env python3
"""Prepare arm64e launchd helpers and a fresh iOS26 data template; no device I/O."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import struct
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def seed_persona_manifest(entries, staged_var):
    directory = '/private/var/keybags'
    manifest = directory + '/persona.kb'
    if not entries.get(directory, 'd\t').startswith('d\t'):
        raise ValueError('The Data keybags path must be a directory')
    if manifest in entries:
        if not entries[manifest].startswith('f\t'):
            raise ValueError('The donor persona manifest must be an ordinary file')
        return
    # The stock early UserManagement task is skipped on the flattened RAMFS
    # boot path. Seed only empty first-boot state in the donor's v1 plist format.
    staged = staged_var / 'keybags/persona.kb'
    staged.parent.mkdir(exist_ok=True)
    staged.write_bytes(plistlib.dumps({'UsePersonaManifestVersion': 1, 'UsePersonaGenerationID': 1,
                                      'UserPersonaDictionary': {}}, fmt=plistlib.FMT_BINARY))
    entries.setdefault(directory, f'd\t700\t0\t0\t{directory}')
    entries[manifest] = f'f\t600\t0\t0\t{manifest}\t/var/keybags/persona.kb'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--reference', type=Path, required=True)
    p.add_argument('--system-root', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True, help='Must not exist')
    args = p.parse_args()
    reference, system, out = args.reference.resolve(), args.system_root.resolve(), args.output.resolve()
    pin = json.loads((ROOT / 'sources.lock.json').read_text())['ios26']['reference']
    version = plistlib.loads((system / 'System/Library/CoreServices/SystemVersion.plist').read_bytes())
    if (version['ProductVersion'], version['ProductBuildVersion']) != ('26.6.2', '23G90'):
        p.error('Expected the pinned iOS26 donor')
    data_root = system / 'private/var'
    for relative, owner in [('.', (0, 0)), ('db', (0, 0)), ('root', (0, 0)), ('mobile', (501, 501))]:
        info = (data_root / relative).lstat()
        if not stat.S_ISDIR(info.st_mode) or (info.st_uid, info.st_gid) != owner:
            p.error('Expected original iOS numeric owners under private/var. Mount the donor with owners enabled.')
    if out.exists():
        p.error('Output exists')
    source = out / 'source'
    source.mkdir(parents=True)
    inputs = {}
    def extract(relative, destination):
        data = subprocess.check_output(['git', '-C', str(reference), 'show', pin['revision'] + ':' + relative])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        inputs[relative] = hashlib.sha256(data).hexdigest()
    prefix = 'userland/userland/'
    for name in ['src/init.c', 'src/inject26.c', 'src/IOMobileFramebuffer.tbd',
                 'libmini/mini.c', 'libmini/mini.h', 'libmini/start.S', 'mkjobs.py']:
        extract(prefix + name, source / name)
    headers = subprocess.check_output(['git', '-C', str(reference), 'ls-tree', '-r', '--name-only',
                                      pin['revision'], prefix + 'src'], text=True).splitlines()
    for name in headers:
        if name.endswith('.h'):
            extract(name, source / name.removeprefix(prefix))
    for name in ('inject-touch.patch', 'inject-wallpaper.patch'):
        patch = ROOT / 'userland/ios26' / name
        subprocess.run(['patch', '-f', '-F', '0', '-p1', '-i', str(patch)], cwd=source, check=True)
        inputs['houji-' + name] = digest(patch)
    # Keep initialization, template setup and exec injection; remove diagnostic
    # exercises and APFS/donor storage selection from the production PID1 path.
    init = source / 'src/init.c'
    text = init.read_text()
    start = text.index('\t{\n\t\tconst char *dev = boot_arg("root_apfs");', text.index('\nmain('))
    end = text.index('\n\tshow_system();', start)
    text = text[:start] + text[end:]
    text = text.replace('\tshow_system();\n', '')
    for name in ['test_files', 'test_writable', 'test_memory', 'test_processes',
                 'test_dynamic', 'test_devices', 'test_time']:
        needle = '\t' + name + '();\n'
        if text.count(needle) != 1:
            raise ValueError('Reference init changed: ' + name)
        text = text.replace(needle, '')
    text = text.replace('static char skeleton[16384];', 'static char skeleton[1024 * 1024];')
    init.write_text(text)
    shutil.copy2(ROOT / 'userland/ios26/stage0.c', source / 'src/houji-stage0.c')
    inputs['houji-stage0.c'] = digest(ROOT / 'userland/ios26/stage0.c')
    inputs['houji-compat-load.c'] = digest(ROOT / 'userland/ios26/compat-load.c')
    helper = out / 'redwood'
    for name in ['sbin', 'lib', 'etc', 'var']:
        (helper / name).mkdir(parents=True)
    sdk = subprocess.check_output(['xcrun', '--sdk', 'iphoneos', '--show-sdk-path'], text=True).strip()
    flags = ['-arch', 'arm64e', '-miphoneos-version-min=26.0', '-Os', '-isysroot', sdk]
    static_flags = [*flags, '-static', '-nostdlib', '-ffreestanding', '-fno-builtin',
                    '-fno-stack-protector', '-I', str(source / 'libmini'),
                    '-Wl,-e,_start', '-Wl,-pagezero_size,0x100000000', '-Wl,-dead_strip']
    commands = []
    def run(command):
        commands.append(command)
        subprocess.run(command, check=True)
    for name, target in [('init', helper / 'sbin/init'), ('houji-stage0', out / 'launchd')]:
        run(['xcrun', 'clang', *static_flags, str(source / 'src' / (name + '.c')),
             str(source / 'libmini/mini.c'), str(source / 'libmini/start.S'), '-o', str(target)])
    library = helper / 'lib/libredwood26.dylib'
    run(['xcrun', 'clang', *flags, '-O1', '-dynamiclib', '-install_name', '/redwood/lib/libredwood26.dylib',
         str(source / 'src/inject26.c'), str(source / 'src/IOMobileFramebuffer.tbd'),
         str(ROOT / 'userland/ios26/compat-load.c'),
         '-framework', 'CoreFoundation', '-framework', 'IOKit', '-framework', 'IOSurface',
         '-lobjc', '-o', str(library)])
    binaries = [out / 'launchd', helper / 'sbin/init', library]
    for binary in binaries:
        magic, cpu, subtype = struct.unpack_from('<III', binary.read_bytes())
        if (magic, cpu, subtype & 0xffffff) != (0xfeedfacf, 0x100000c, 2):
            raise ValueError('Helper is not arm64e')
        run(['codesign', '--force', '--sign', '-', '--timestamp=none', str(binary)])
        run(['codesign', '--verify', '--strict', str(binary)])
    (helper / 'etc/rc').write_text('exec /sbin/launchd.ios\n')
    (helper / 'etc/inject.conf').write_text('/redwood/lib/libredwood26.dylib backboardd SpringBoard PosterBoard\n')
    (helper / 'etc/motd').write_text('Houji iOS26 arm64e bring-up\n')
    # A fresh donor template. Files are copied as ordinary bytes (HFS compression
    # is disabled in the kernel); owners and modes are applied by init to ramfs.
    entries = {}
    def template_walk_error(error):
        raise ValueError('Cannot enumerate the donor Data template: ' + str(error)) from error

    def template_directory(path):
        info = path.lstat()
        relative = path.relative_to(data_root)
        destination = '/private/var' + (('/' + str(relative)) if relative != Path('.') else '')
        mode = format(stat.S_IMODE(info.st_mode), 'o')
        if stat.S_ISDIR(info.st_mode):
            entries[destination] = f'd\t{mode}\t{info.st_uid}\t{info.st_gid}\t{destination}'
        elif stat.S_ISLNK(info.st_mode):
            entries[destination] = f'l\t{os.readlink(path)}\t{destination}'
        elif stat.S_ISREG(info.st_mode):
            copy = helper / 'var' / relative
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, copy)
            entries[destination] = f'f\t{mode}\t{info.st_uid}\t{info.st_gid}\t{destination}\t/var/{relative}'
        else:
            raise ValueError('Unsupported template entry: ' + str(relative))
    template_directory(data_root)
    for parent, directories, files in os.walk(data_root, followlinks=False, onerror=template_walk_error):
        if Path(parent) == data_root:
            directories[:] = [d for d in directories if d != 'staged_system_apps']
        for name in sorted(directories + files):
            template_directory(Path(parent) / name)
    seed_persona_manifest(entries, helper / 'var')
    paths = subprocess.check_output(['git', '-C', str(reference), 'ls-tree', '-r', '--name-only',
                                     pin['revision'], prefix + 'var'], text=True).splitlines()
    for path in paths:
        relative = Path(path.removeprefix(prefix + 'var/'))
        destination = helper / 'var' / relative
        extract(path, destination)
        uid = gid = 501 if relative.parts[0] == 'mobile' else 0
        entries['/private/var/' + str(relative)] = f'f\t644\t{uid}\t{gid}\t/private/var/{relative}\t/var/{relative}'
    preference = helper / 'var/mobile/Library/Preferences/com.apple.purplebuddy.plist'
    prefs = plistlib.loads(preference.read_bytes())
    prefs.update(SetupDone=True, SetupFinishedAllSteps=True, SetupVersion=99, ForceNoBuddy=True,
                 SetupState='SetupUsingAssistant', SetupLastExit=datetime.datetime(2026, 11, 6))
    preference.write_bytes(plistlib.dumps(prefs, fmt=plistlib.FMT_BINARY))
    migration = plistlib.loads((helper / 'var/mobile/Library/Preferences/com.apple.migration.plist').read_bytes())
    migration['LastSystemVersion'] = version['ProductBuildVersion']
    for user, uid in [('root', 0), ('mobile', 501)]:
        for name in ['com.apple.migration.plist', 'com.apple.DataMigration.plist',
                     'ByHost/com.apple.migration..plist', 'ByHost/com.apple.migration.plist',
                     'ByHost/com.apple.DataMigration..plist', 'ByHost/com.apple.DataMigration.plist']:
            relative = Path(user) / 'Library/Preferences' / name
            target = helper / 'var' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(plistlib.dumps(migration, fmt=plistlib.FMT_BINARY))
            entries['/private/var/' + str(relative)] = f'f\t644\t{uid}\t{uid}\t/private/var/{relative}\t/var/{relative}'
    # Ensure new preference parent directories precede all file records.
    for name in list(entries):
        parent = Path(name).parent
        while str(parent).startswith('/private/var/'):
            if str(parent) not in entries:
                uid = 501 if str(parent).startswith('/private/var/mobile') else 0
                entries[str(parent)] = f'd\t755\t{uid}\t{uid}\t{parent}'
            parent = parent.parent
    ordered = sorted(entries.values(), key=lambda line: (line[0] != 'd', line.count('/'), line))
    skeleton = '\n'.join(ordered) + '\n'
    if len(skeleton.encode()) >= 1024 * 1024 or any('\0' in line for line in ordered):
        raise ValueError('Data template exceeds init bounds')
    (helper / 'etc/var.skel').write_text(skeleton)
    files = {str(f.relative_to(out)): digest(f) for f in sorted(helper.rglob('*')) if f.is_file()}
    files['launchd'] = digest(out / 'launchd')
    report = {'architecture': 'arm64e', 'deployment_target': '26.0', 'reference_revision': pin['revision'],
              'donor_build': '23G90', 'stage0': 'direct HFS helper; no donor storage or md1 mount',
              'data_mode': 'fresh ramfs template; persistence not yet qualified',
              'data_numeric_owners_verified': True,
              'runtime_collectors': False, 'files': files, 'source_inputs': inputs,
              'device_accessed': False, 'installed': False, 'hardware_verified': False}
    (out / 'commands.json').write_text(json.dumps(commands, indent=2) + '\n')
    (out / 'build-result.json').write_text(json.dumps(report, indent=2) + '\n')
    print('Built signed arm64e init/stage0/injection helpers and iOS26 data template; not installed')


if __name__ == '__main__':
    main()
