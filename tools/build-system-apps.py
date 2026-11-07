#!/usr/bin/env python3
"""Build the board system-app registration helper. No image or device writes."""
from pathlib import Path
import argparse
import hashlib
import json
import plistlib
import subprocess

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--profile', choices=('ios16', 'ios26'), default='ios16')
parser.add_argument('--system-root', type=Path, help='Read-only donor root required for the iOS26 catalog')
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
if args.profile == 'ios26' and args.system_root is None:
    parser.error('iOS26 requires --system-root; never reuse the iOS16 app catalog')
out = args.output.resolve()
out.mkdir(parents=True, exist_ok=False)
source = ROOT / 'userland/apps'
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
inputs = {str(p.relative_to(ROOT)): sha(p) for p in source.iterdir() if p.is_file()}
catalog = json.loads((source / 'system-apps.json').read_text())
if args.profile == 'ios26':
    applications = {}
    bundle_names = {}
    for relative in ('Applications', 'private/var/staged_system_apps'):
        directory = args.system_root.resolve() / relative
        if not directory.is_dir() or directory.is_symlink():
            continue
        for bundle in sorted(directory.glob('*.app')):
            info = bundle / 'Info.plist'
            if bundle.is_symlink() or not info.is_file() or info.is_symlink():
                continue
            metadata = plistlib.loads(info.read_bytes())
            identifier = metadata.get('CFBundleIdentifier')
            executable = metadata.get('CFBundleExecutable')
            if not isinstance(identifier, str) or not identifier.startswith('com.apple.') or not isinstance(executable, str):
                continue
            if '/' in executable or executable in ('.', '..') or not (bundle / executable).is_file():
                continue
            if identifier in bundle_names and bundle_names[identifier] != bundle.name:
                raise ValueError('Ambiguous stock application identifier: ' + identifier)
            if bundle.name in applications and applications[bundle.name] != identifier:
                raise ValueError('Ambiguous stock application directory: ' + bundle.name)
            bundle_names[identifier] = bundle.name
            applications[bundle.name] = identifier
    if not applications:
        raise ValueError('No original iOS26 application bundles found')
    catalog = {'applications': applications}
    (out / 'donor-apps.json').write_text(json.dumps(catalog, indent=2) + '\n')

identifier = hashlib.sha256(json.dumps(inputs if args.profile == 'ios16' else {'source': inputs, 'catalog': catalog}, sort_keys=True).encode()).hexdigest()
entries = ',\n'.join('@' + json.dumps(k) + ':@' + json.dumps(v) for k,v in catalog['applications'].items())
(out / 'SystemAppsManifest.h').write_text('static NSString *const kSystemAppsManifest=@"' + identifier + '";\n'
    'static NSDictionary *expectedSystemApps(void){return @{\n' + entries + '\n};}\n')
sdk = subprocess.check_output(['xcrun','--sdk','iphoneos','--show-sdk-path'], text=True).strip()
binary = out / 'houji-system-apps'
command = ['xcrun','clang','-target',('arm64e-apple-ios26.0' if args.profile == 'ios26' else 'arm64-apple-ios16.0'),'-isysroot',sdk,'-O2',
           '-fstack-protector-strong','-fobjc-arc','-Wall','-Wextra','-Werror',
           '-Wno-deprecated-declarations','-I',str(out),str(source/'HoujiSystemApps.m'),
           '-framework','Foundation','-o',str(binary)]
if args.profile == 'ios26':
    command.insert(2, '-DHOUJI_SYSTEM_APPS_IOS26=1')
(out/'compile-command.json').write_text(json.dumps(command,indent=2)+'\n')
with (out/'compile.log').open('w') as log:
    subprocess.run(command,check=True,stdout=log,stderr=subprocess.STDOUT)
subprocess.run(['codesign','--force','--sign','-','--timestamp=none','--entitlements',
                str(source/'entitlements.plist'),str(binary)],check=True)
subprocess.run(['codesign','--verify','--strict',str(binary)],check=True)
assert all(sha(ROOT/name)==digest for name,digest in inputs.items())
(out/'build-result.json').write_text(json.dumps({'status':'BUILT_NOT_RUNTIME_VERIFIED',
    'source_inputs':inputs,'manifest':identifier,'binary_sha256':sha(binary),
    'profile':args.profile, 'architecture':'arm64e' if args.profile == 'ios26' else 'arm64',
    'expected_system_apps':len(catalog['applications']),'phone_accessed':False},indent=2)+'\n')
print('Built and signed system-app registrar; actual OS registration is unverified')
