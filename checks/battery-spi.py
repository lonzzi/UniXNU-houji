#!/usr/bin/env python3
from pathlib import Path
import subprocess
import tempfile
ROOT=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='houji-spi-') as temp:
    for fixture in ('battery-spi', 'ios26-spi'):
        binary=Path(temp)/fixture
        subprocess.run(['clang','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined',
                        '-I',str(ROOT/'kernel/overlay/pexpert/arm'),
                        str(ROOT/'checks'/f'{fixture}.c'),'-o',str(binary)],check=True)
        subprocess.run([str(binary)],check=True)
