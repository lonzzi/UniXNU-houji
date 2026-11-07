#!/usr/bin/env python3
"""Check the production image author's path and disk-image access guards."""
import importlib.util
import os
from pathlib import Path
import plistlib
import stat
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ios26_root', ROOT / 'tools/build-ios26-root.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
spec = importlib.util.spec_from_file_location('ios26_userland', ROOT / 'tools/build-ios26-userland.py')
userland = importlib.util.module_from_spec(spec)
spec.loader.exec_module(userland)


def rejected(function, *args):
    try:
        function(*args)
    except ValueError:
        return
    raise AssertionError('Unsafe image operation accepted')


with tempfile.TemporaryDirectory(prefix='houji26-root-') as temporary:
    base = Path(temporary)
    image, outside = base / 'image', base / 'outside'
    image.mkdir();outside.mkdir()
    sentinel = outside / 'retained'
    sentinel.write_text('must survive')
    (image / 'redirect').symlink_to(outside, target_is_directory=True)
    rejected(module.directory_in_root, image, 'redirect/subdirectory')
    rejected(module.directory_in_root, image, '../outside')
    rejected(module.directory_in_root, image, str(outside))
    assert not (outside / 'subdirectory').exists()
    replacement = module.replace_directory(image, 'redirect')
    assert replacement.is_dir() and not replacement.is_symlink() and sentinel.read_text() == 'must survive'
    real = image / 'ordinary'
    real.write_bytes(b'data')
    module.ordinary_file(real)
    alias = image / 'alias'
    alias.symlink_to(real)
    rejected(module.ordinary_file, alias)
    rejected(module.ordinary_file, Path('/dev/null'))
    for name in ('OS', 'App'):
        (image / 'private/preboot/Cryptexes' / name).mkdir(parents=True)
    (image / 'System/Cryptexes').mkdir(parents=True)
    for name in ('OS', 'App'):
        (image / 'System/Cryptexes' / name).symlink_to('../../private/preboot/Cryptexes/' + name)
    cache = image / 'private/preboot/Cryptexes/OS/System/Library/Caches/com.apple.dyld'
    cache.mkdir(parents=True)
    main_cache = cache / 'dyld_shared_cache_arm64e'
    main_cache.write_bytes(b'dyld_v1  arm64e\0\0')
    rejected(module.verify_cryptex_views, image)
    mask = os.umask(0o077)
    try:
        module.install_shared_cache_view(image)
    finally:
        os.umask(mask)
    module.verify_cryptex_views(image)
    fallback = image / 'System/Library/Caches/com.apple.dyld'
    assert stat.S_IMODE(fallback.lstat().st_mode) & 0o555 == 0o555
    assert (fallback / main_cache.name).read_bytes() == main_cache.read_bytes()
    module.install_shared_cache_view(image)
    fallback.unlink()
    fallback.mkdir()
    (fallback / 'retained').write_text('must survive')
    rejected(module.install_shared_cache_view, image)
    assert (fallback / 'retained').read_text() == 'must survive'
    (fallback / 'retained').unlink();fallback.rmdir()
    fallback.symlink_to(outside, target_is_directory=True)
    rejected(module.install_shared_cache_view, image)
    rejected(module.verify_cryptex_views, image)
    fallback.unlink()
    module.install_shared_cache_view(image)
    (image / 'System/Cryptexes/OS').unlink()
    (image / 'System/Cryptexes/OS').symlink_to(str(outside))
    rejected(module.verify_cryptex_views, image)
    (image / 'System/Cryptexes/OS').unlink()
    (image / 'System/Cryptexes/OS').symlink_to('../../private/preboot/Cryptexes/OS')
    main_cache.write_bytes(b'dyld_v1   arm64\0\0')
    rejected(module.verify_cryptex_views, image)
    valid = {'BusProtocol': 'Disk Image', 'MountPoint': str(image), 'Writable': False}
    with patch.object(module, 'run', return_value=plistlib.dumps(valid)):
        module.mounted_image(image, False)
        rejected(module.mounted_image, image, True)
    for changed in [{'BusProtocol': 'USB'}, {'BusProtocol': 'PCI-Express'},
                    {'MountPoint': '/'}, {'Writable': True}]:
        with patch.object(module, 'run', return_value=plistlib.dumps({**valid, **changed})):
            rejected(module.mounted_image, image, False)
    staged = base / 'var'
    staged.mkdir()
    entries = {}
    userland.seed_persona_manifest(entries, staged)
    persona = staged / 'keybags/persona.kb'
    assert plistlib.loads(persona.read_bytes()) == {
        'UsePersonaManifestVersion': 1, 'UsePersonaGenerationID': 1, 'UserPersonaDictionary': {}}
    directory, path = '/private/var/keybags', '/private/var/keybags/persona.kb'
    assert entries[directory].split('\t') == ['d', '700', '0', '0', directory]
    assert entries[path].split('\t') == ['f', '600', '0', '0', path, '/var/keybags/persona.kb']
    persona.write_bytes(b'preserve donor manifest')
    userland.seed_persona_manifest(entries, staged)
    assert persona.read_bytes() == b'preserve donor manifest'
    rejected(userland.seed_persona_manifest, {directory: 'l\t/tmp\t' + directory}, staged)
print('PASS: production image guards reject physical media, wrong access and symlink escapes; no HFS or device emulation')
print('PASS: shared-cache alias stays readable and RAMFS persona bootstrap preserves an existing manifest')
