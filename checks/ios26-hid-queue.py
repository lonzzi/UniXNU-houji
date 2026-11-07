#!/usr/bin/env python3
"""Verify the actual native touch serializer against the 23G90 queue layout."""
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HID = Path('iokit/Kernel/arm/hid')
legacy = ROOT / 'kernel/overlay' / HID
modern = ROOT / 'kernel/ios26/overlay' / HID
data = (legacy / 'IOHIDEventData.h').read_text()
base = data[data.index('#define IOHIDEVENT_BASE'):data.index('#define IOHIDAXISEVENT_BASE')]
header = re.search(r'typedef struct [^\n]* _IOHIDSystemQueueElement \{.*?\} IOHIDSystemQueueElement;', data, re.S)[0]
records = (legacy / 'IOHIDEventStructDefs.h').read_text().split('typedef struct {')
digitizer = 'typedef struct {' + next(record for record in records if '} IOHIDDigitizerEventData;' in record)
source = (legacy / 'IOHIDEvent.cpp').read_text()
start = source.index('IOByteCount IOHIDEvent::serviceQueueBytes(')
serializer = source[start:source.index('#endif', start)]
unit = r'''
#include <cassert>
#include <cstdint>
#include <cstring>
#include <initializer_list>
#include <vector>
using IOHIDEventType = uint32_t;
using IOFixed = int32_t;
using UInt8 = uint8_t;
using UInt32 = uint32_t;
using IOByteCount = uint64_t;
constexpr IOFixed kIOFixedNaN = INT32_MIN;
constexpr unsigned kIOHIDEventTypeDigitizer = 11;
enum { kIOHIDDigitizerOrientationTypeTilt, kIOHIDDigitizerOrientationTypePolar,
       kIOHIDDigitizerOrientationTypeQuality };
''' + base + header + digitizer + r'''
struct IOHIDEventData { IOHIDEVENT_BASE };
#include "HoujiDigitizer26.h"

struct IOHIDEvent;
struct Children : std::vector<const IOHIDEvent *> {
    using std::vector<const IOHIDEvent *>::vector;
    unsigned getCount() const { return size(); }
    const IOHIDEvent *getObject(unsigned index) const { return at(index); }
};
struct IOHIDEvent {
    const IOHIDEventData *_data;
    const Children *_children;
    uint64_t _senderID, _timeStamp;
    uint32_t _options = 0;
    IOByteCount serviceQueueBytes(void *, IOByteCount) const;
    IOByteCount writeServiceQueueBytes(UInt8 *, IOByteCount, uint64_t, unsigned, UInt32 &) const;
};
''' + serializer + r'''
template <class T> T read(const uint8_t *bytes) {
    T result;
    memcpy(&result, bytes, sizeof(result));
    return result;
}

int main() {
    constexpr uint32_t recordSize = 0x1c + 0xa0;
    IOHIDDigitizerEventData hand = {}, finger = {}, second = {};
    hand.size = finger.size = second.size = sizeof(hand);
    hand.type = finger.type = second.type = kIOHIDEventTypeDigitizer;
    hand.options.collection = hand.options.displayIntegrated = hand.options.touch = 1;
    finger.options.touch = finger.options.displayIntegrated = 1;
    hand.transducerType = 3;
    finger.transducerType = second.transducerType = 2;
    finger.identity = 2;
    second.identity = 3;
    finger.position.x = 32768;
    finger.position.y = 49152;
    second.position.y = -16384;
    second.pressure = kIOFixedNaN;
    second.generationCount = 9;
    second.didUpdateMask = 5;
    const IOHIDEvent one{reinterpret_cast<IOHIDEventData *>(&finger), nullptr, 0, 42};
    const IOHIDEvent two{reinterpret_cast<IOHIDEventData *>(&second), nullptr, 0, 42};
    const Children children{&one, &two};
    const IOHIDEvent parent{reinterpret_cast<IOHIDEventData *>(&hand), &children, 17, 42};
    constexpr uint32_t length = recordSize * 3;
    alignas(8) uint8_t bytes[length + 5];
    memset(bytes, 0x5a, sizeof(bytes));
    assert(!parent.serviceQueueBytes(nullptr, length - 1));
    assert(parent.serviceQueueBytes(nullptr, length) == length);
    assert(parent.serviceQueueBytes(bytes + 4, length) == length);
    assert(bytes[3] == 0x5a && bytes[length + 4] == 0x5a);
    for (unsigned i = 0; i < 3; ++i) {
        const uint8_t *record = bytes + 4 + i * recordSize;
        assert(read<uint64_t>(record) == 42 && read<uint64_t>(record + 8) == 17);
        assert(read<uint32_t>(record + 0x14) == 0);
        assert(read<uint32_t>(record + 0x18) == (i == 0 ? 3 : i));
        const uint8_t *payload = record + 0x1c;
        assert(read<uint32_t>(payload) == 0xa0 && read<uint32_t>(payload + 4) == 11);
        assert(payload[0xc] == (i == 0 ? 0 : 1));
        if (i == 1) {
            assert(read<uint32_t>(payload + 0x30) == 2);
            assert(read<double>(payload + 0x10) == 0.5);
            assert(read<double>(payload + 0x18) == 0.75);
        }
        if (i == 2) {
            assert(read<uint32_t>(payload + 0x30) == 3);
            assert(read<double>(payload + 0x18) == -0.25);
            assert(read<uint64_t>(payload + 0x40) == 0x7ff8000000000000ULL);
            assert(read<uint32_t>(payload + 0x90) == 9 && read<uint32_t>(payload + 0x98) == 5);
        }
    }
}
'''
with tempfile.TemporaryDirectory(prefix='houji26-hid-queue-') as temporary:
    cpp = Path(temporary) / 'queue.cpp'
    binary = Path(temporary) / 'queue'
    cpp.write_text(unit)
    subprocess.run(['clang++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                    '-fno-strict-aliasing', '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                    '-I', str(modern), str(cpp), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
print('PASS: actual 23G90 queue serializer preserves finger identity, depth, bounds and binary64 coordinates; no hardware')
