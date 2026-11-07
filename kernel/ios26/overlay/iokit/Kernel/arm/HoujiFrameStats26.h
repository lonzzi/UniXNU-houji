/* SPDX-License-Identifier: BSD-2-Clause-Patent */
#ifndef HOUJI_FRAME_STATS26_H
#define HOUJI_FRAME_STATS26_H
#include <stdint.h>

/* A presenter owns these counters under its display lock. Retain intervals
 * only across recent touch activity, including stalls between those frames. */
struct HoujiFrameStats26 {
    uint64_t copies = 0, lastCopyUs = 0, interactiveUs = 0;
    uint64_t queueUs = 0, fenceUs = 0, cacheUs = 0, writeUs = 0, maxWriteUs = 0;
    uint32_t within[5] = {};
    uint64_t lastBurst = 0;
    uint64_t stretchedLayers = 0;
    uint32_t layer = 0, sourceWidth = 0, sourceHeight = 0, destWidth = 0, destHeight = 0;

    void recordLayer(unsigned index, uint32_t sw, uint32_t sh, uint32_t dw, uint32_t dh)
    {
        const uint64_t a = (uint64_t) sw * dh, b = (uint64_t) sh * dw;
        const uint64_t difference = a > b ? a - b : b - a;
        // Ignore ordinary pixel rounding below one percent.
        if (difference <= (a > b ? a : b) / 100) return;
        ++stretchedLayers;
        layer = index;
        sourceWidth = sw;
        sourceHeight = sh;
        destWidth = dw;
        destHeight = dh;
    }

    void record(uint64_t completeUs, uint64_t queuedUs, uint64_t waitingUs,
        uint64_t acquiringUs, uint64_t drawingUs, uint64_t touchBurst)
    {
        if (touchBurst && touchBurst == lastBurst && completeUs > lastCopyUs) {
            const uint64_t gap = completeUs - lastCopyUs;
            constexpr uint32_t limits[] = {8600, 12500, 17000, 25000};
            unsigned bucket = 0;
            while (bucket < sizeof(limits) / sizeof(limits[0]) && gap > limits[bucket]) ++bucket;
            ++within[bucket];
            interactiveUs += gap;
        }
        lastCopyUs = completeUs;
        lastBurst = touchBurst;
        ++copies;
        queueUs += queuedUs;
        fenceUs += waitingUs;
        cacheUs += acquiringUs;
        writeUs += drawingUs;
        if (drawingUs > maxWriteUs) maxWriteUs = drawingUs;
    }
};
#endif
