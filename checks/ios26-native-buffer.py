#!/usr/bin/env python3
"""Exercise the patched native mdev completion path with buffer and UFS peers."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
patch = (ROOT / 'kernel/ios26/patches/local.patch').read_text()
memdev = patch.split('diff --git a/bsd/dev/memdev.c ', 1)[1].split('\ndiff --git ', 1)[0]
postimage = '\n'.join(line[1:] for line in memdev.splitlines()
                      if line.startswith(('+', ' ')) and not line.startswith('+++'))
begin = postimage.index('\tif (mdev[devid].mdOps != NULL) {\n\t\tcaddr_t buffer')
end = postimage.index('\tif (buf_map(bp,', begin)
source = r'''
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
typedef char *caddr_t;
enum { B_READ = 1 };
struct buf { unsigned count, flags, resid, done; int error; };
static char storage[512];
static int map_error, transfer_error, unmap_error;
static unsigned maps, transfers, unmaps, write_direction, mapped;
static unsigned buf_count(struct buf *bp) { return bp->count; }
static unsigned buf_flags(struct buf *bp) { return bp->flags; }
static void buf_seterror(struct buf *bp, int error) { bp->error = error; }
static void buf_setresid(struct buf *bp, unsigned resid) { bp->resid = resid; }
static void buf_biodone(struct buf *bp) { assert(!mapped); ++bp->done; }
static int buf_map_range(struct buf *bp, caddr_t *buffer) {
    assert(bp->count == 512 && !mapped);
    ++maps;
    if (map_error) return map_error;
    mapped = 1;
    *buffer = storage;
    return 0;
}
static int buf_unmap_range(struct buf *bp) {
    assert(bp->count == 512 && mapped);
    ++unmaps;
    mapped = 0;
    return unmap_error;
}
static int transfer(void *context, uint64_t offset, void *buffer, unsigned bytes, int write) {
    assert(context == storage && offset == 4096 && buffer == storage && bytes == 512 && mapped);
    ++transfers;
    write_direction = (unsigned)write;
    return transfer_error;
}
struct mdev_ops { int (*transfer)(void *, uint64_t, void *, unsigned, int); };
static const struct mdev_ops ops = { transfer };
static const struct { const struct mdev_ops *mdOps; void *mdContext; } mdev[] = {{ &ops, storage }};
static void strategy(struct buf *bp) {
    const unsigned devid = 0;
    const uint64_t blkoff = 4096;
'''
source += postimage[begin:end] + r'''
    assert(0);
}
static void check(unsigned count, unsigned flags, int map, int io, int unmap, int expected) {
    struct buf b = { .count = count, .flags = flags, .resid = count };
    maps = transfers = unmaps = 0;
    map_error = map;
    transfer_error = io;
    unmap_error = unmap;
    strategy(&b);
    assert(b.done == 1 && b.error == expected && b.resid == (expected ? count : 0));
    assert(maps == (count != 0) && transfers == (count != 0 && !map) && unmaps == transfers);
    if (transfers) assert(write_direction == !(flags & B_READ));
}
int main(void) {
    check(0, B_READ, 0, 0, 0, 0);
    check(512, B_READ, 0, 0, 0, 0);
    check(512, 0, 0, 0, 0, 0);
    check(512, B_READ, ENOMEM, 0, 0, ENOMEM);
    check(512, 0, 0, EIO, 0, EIO);
    check(512, B_READ, 0, 0, EINVAL, EINVAL);
    check(512, B_READ, 0, EIO, EINVAL, EIO);
    puts("PASS: native mdev range map/unmap balance and one completion on each error; no VM emulation");
}
'''
with tempfile.TemporaryDirectory(prefix='houji26-native-buffer-') as tmp:
    fixture, binary = Path(tmp) / 'buffer.c', Path(tmp) / 'buffer'
    fixture.write_text(source)
    subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                    str(fixture), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
