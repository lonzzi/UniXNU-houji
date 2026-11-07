#!/usr/bin/env python3
"""Run portable fixtures and Apple runtime checks; does not emulate a GPU."""
import argparse
from pathlib import Path
import platform
import subprocess
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]
APPLE_CHECKS = ('metal-trace-id.py', 'metal-publication.py', 'gpu-stage-trace.py', 'metal-surface-use.py', 'metal-async.py', 'metal-command-dictionary.py', 'statusbar-geometry.py')
CHECKS = (
    'cpu-lut.py',
    'gpu-input-prewarm.py',
    'cpu-snapshot.py',
    'backlight-properties.py',
    'battery-survey.py',
    'battery-sparse-survey.py',
    'battery-protocol.py',
    'battery-startup.py',
    'battery-firmware.py',
    'battery-cmd-db.py',
    'battery-spi.py',
    'battery-adsp-codecs.py',
    'battery-adsp-policy.py',
    'battery-publisher.py',
    'turnip-bo-retire.py',
    'turnip-bo-cache.py',
    'native-gpu/run.py',
    'native-gpu/transport.py',
    'native-gpu/board-gate.py',
    'native-gpu/dispatch-gate.py',
    'native-gpu/fence-poll.py',
    'gpu-address-space.py',
    'gpu-client-slots.py',
    'gpu-coherence.py',
    'gpu-latency.py',
    'gpu-issuer-trace.py',
    'metal-constants.py',
    'texture-properties.py',
    'metal-render-pass.py',
    'gpu-memory-cache.py',
    'gpu-room-worker.py',
    'gpu-stage-trace.py',
    'metal-device-answers.py',
    'metal-device-limits.py',
    'metal-no-copy-pages.py',
    'metal-surface-use.py',
    'display-queue.py',
    'display-snapshot.py',
    'display-timing.py',
    'boot-arguments.py',
    'ios26-pac.py',
    'ios26-entry.py',
    'ios26-entropy.py',
    'ios26-trampoline.py',
    'ios26-root.py',
    'ios26-native-buffer.py',
    'ios26-system.py',
    'ios26-startup-results.py',
    'ios26-hid-queue.py',
    'ios26-coherence-fallback.py',
    'ios26-firmware-writes.py',
    'ios26-return.py',
    'ios26-present.py',
    'build-inputs.py',
    'recovery-logs.py',
    'userland-logs.py',
    'metal-async.py',
    'metal-trace-id.py',
    'network-stack.py',
    'system-apps.py',
    'system-app-dyld.py',
    'tcc-media-compatibility.py',
    'metal-command-dictionary.py',
    'metal-publication.py',
    'statusbar-geometry.py',
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=('auto', 'portable', 'apple', 'all'), default='auto',
                        help='auto runs all on Apple Silicon, portable elsewhere')
    suite = parser.parse_args().suite
    apple_host = platform.system() == 'Darwin' and platform.machine() == 'arm64'
    if suite == 'auto':
        suite = 'all' if apple_host else 'portable'
    if suite in ('apple', 'all') and not apple_host:
        parser.error('Apple runtime checks require an Apple Silicon macOS host with Xcode')
    if platform.machine() not in ('arm64', 'aarch64'):
        parser.error('Host checks compile AArch64 SMMU bindings; use an arm64 host')
    print(f'Host check suite: {suite} ({platform.system()} {platform.machine()})', flush=True)
    if suite == 'portable':
        print('Apple runtime checks are not covered here; run --suite all on Apple Silicon macOS.', flush=True)
    if suite != 'apple':
        with tempfile.TemporaryDirectory(prefix='houji-check-') as temporary:
            for filename, compiler, standard in [
                ('gpu-governor.cpp', 'clang++', 'c++17'),
                ('gpu-hfi.c', 'clang', 'c11'),
                ('cpu-governor.cpp', 'clang++', 'c++17'),
                ('power-display.cpp', 'clang++', 'c++17'),
                ('sample-retention.c', 'clang', 'c11'),
            ]:
                binary = Path(temporary) / Path(filename).stem
                subprocess.run([compiler, '-std=' + standard, '-Wall', '-Wextra', '-Werror', str(ROOT / 'checks' / filename), '-o', str(binary)], check=True)
                subprocess.run([str(binary)], check=True)
    selected = [name for name in CHECKS
                if suite == 'all' or (name in APPLE_CHECKS) == (suite == 'apple')]
    for name in selected:
        print('CHECK: ' + name, flush=True)
        subprocess.run([sys.executable, str(ROOT / 'checks' / name)], check=True)
    print(f'PASS: {suite} suite completed; no GPU or frame-rate qualification', flush=True)


if __name__ == '__main__':
    main()
