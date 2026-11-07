/* SPDX-License-Identifier: APSL-2.0 */
#pragma once

// 23G90 IOKit reads the native queue's digitizer payload in this layout.
// Binary64 values are stored as bits so the kernel never touches FP state.
struct Houji26DigitizerData {
    uint32_t size, type, options;
    uint8_t depth, reserved[3];
    uint64_t x, y, z;
    uint32_t transducerIndex, transducerType, identity, eventMask, childEventMask, buttonMask;
    uint64_t pressure, auxPressure, twist;
    uint32_t orientationType, orientationPadding;
    uint64_t orientation[6];
    uint32_t generationCount, willUpdateMask, didUpdateMask, padding;
};
static_assert(sizeof(IOHIDSystemQueueElement) == 0x1c);
static_assert(sizeof(Houji26DigitizerData) == 0xa0);
static_assert(__builtin_offsetof(Houji26DigitizerData, x) == 0x10);
static_assert(__builtin_offsetof(Houji26DigitizerData, identity) == 0x30);
static_assert(__builtin_offsetof(Houji26DigitizerData, pressure) == 0x40);
static_assert(__builtin_offsetof(Houji26DigitizerData, orientation) == 0x60);
static_assert(__builtin_offsetof(Houji26DigitizerData, generationCount) == 0x90);

static uint64_t houji26FixedToBinary64(IOFixed value)
{
    if (value == kIOFixedNaN) return 0x7ff8000000000000ULL;
    if (!value) return 0;
    uint32_t magnitude = value < 0 ? -static_cast<int64_t>(value) : value;
    unsigned top = 31U - __builtin_clz(magnitude);
    uint64_t sign = value < 0 ? 1ULL << 63 : 0;
    uint64_t exponent = static_cast<uint64_t>(1023U - 16U + top) << 52;
    uint64_t fraction = (static_cast<uint64_t>(magnitude) << (52U - top)) & ((1ULL << 52) - 1);
    return sign | exponent | fraction;
}

static void houji26WriteDigitizer(void *bytes, const IOHIDDigitizerEventData &event, uint8_t depth)
{
    Houji26DigitizerData data = {};
    data.size = sizeof(data);
    data.type = event.type;
    memcpy(&data.options, &event.options, sizeof(data.options));
    data.depth = depth;
    data.x = houji26FixedToBinary64(event.position.x);
    data.y = houji26FixedToBinary64(event.position.y);
    data.z = houji26FixedToBinary64(event.position.z);
    data.transducerIndex = event.transducerIndex;
    data.transducerType = event.transducerType;
    data.identity = event.identity;
    data.eventMask = event.eventMask;
    data.childEventMask = event.childEventMask;
    data.buttonMask = event.buttonMask;
    data.pressure = houji26FixedToBinary64(event.pressure);
    data.auxPressure = houji26FixedToBinary64(event.auxPressure);
    data.twist = houji26FixedToBinary64(event.twist);
    data.orientationType = event.orientationType;
    // Tilt has two values. Polar and quality use all six slots in union order.
    unsigned orientationCount = 0;
    if (event.orientationType == kIOHIDDigitizerOrientationTypeTilt) orientationCount = 2;
    if (event.orientationType == kIOHIDDigitizerOrientationTypePolar ||
        event.orientationType == kIOHIDDigitizerOrientationTypeQuality) orientationCount = 6;
    IOFixed orientation[6];
    memcpy(orientation, &event.orientation, sizeof(orientation));
    for (unsigned i = 0; i < orientationCount; ++i) {
        data.orientation[i] = houji26FixedToBinary64(orientation[i]);
    }
    data.generationCount = event.generationCount;
    data.willUpdateMask = event.willUpdateMask;
    data.didUpdateMask = event.didUpdateMask;
    memcpy(bytes, &data, sizeof(data));
}
