/* SPDX-License-Identifier: BSD-2-Clause-Patent */
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <vector>
#include <type_traits>
static int nextToken;
static bool held;
static unsigned acquired,released;
extern "C" int native_adreno_present_begin_frame(uint32_t fence) {
    assert((fence==42 || fence==0) && !held);++acquired;held=nextToken>0;return nextToken;
}
extern "C" uint32_t native_adreno_present_capture_fence(void) { return 42; }
extern "C" void native_adreno_present_end(void) { assert(held);held=false;++released; }
#include "HoujiPresent26.h"
#include "HoujiFrameStats26.h"
struct Cache {
    unsigned attr[3]={0xff,0x44,0xff};
    unsigned barriers=0;
    std::vector<uintptr_t> cleaned;
    unsigned attribute(uintptr_t address) { assert(address>=0x4000 && address<0x10000);return attr[(address-0x4000)/16384]; }
    void cleanInvalidate(uintptr_t address) { assert(held && !(address&63));cleaned.push_back(address); }
    void barrier() { assert(held);++barriers; }
};
int main() {
    static_assert(!std::is_copy_constructible<HoujiPresentLease26>::value,"unique gate ownership");
    for(int token:{-1,0,1}) for(uint32_t fence:{0U,42U}) {
        nextToken=token;acquired=released=0;
        {
            HoujiPresentLease26 lease(fence);
            assert(lease.ready()==(token>0 || (token==0 && fence==0)) && lease.gpu()==(token>0));
            assert(held==(token>0) && !released);
        }
        assert(!held && acquired==1 && released==unsigned(token>0));
    }
    nextToken=1;
    {
        HoujiPresentLease26 lease(42);Cache cache;
        assert(houji26AcquirePixels(0x4000,49152,0x7ff0,48,64,16384,cache));
        assert(cache.cleaned==std::vector<uintptr_t>{0x7fc0} && cache.barriers==1);
        Cache bad;bad.attr[1]=0;
        assert(!houji26AcquirePixels(0x4000,49152,0x7ff0,48,64,16384,bad));
        assert(bad.cleaned.empty() && !bad.barriers);
        for(auto &attribute:bad.attr)attribute=0x44;
        assert(houji26AcquirePixels(0x4000,49152,0x4000,49152,64,16384,bad));
        assert(bad.cleaned.empty() && bad.barriers==1);
        for(unsigned line:{0,1,48,4096}) {
            Cache invalid;assert(!houji26AcquirePixels(0x4000,49152,0x7ff0,48,line,16384,invalid));
            assert(invalid.cleaned.empty() && !invalid.barriers);
        }
        Cache bounds;
        assert(!houji26AcquirePixels(0x4001,63,0x4001,1,64,16384,bounds));
        assert(!houji26AcquirePixels(0x4000,49152,0xffff,2,64,16384,bounds));
        assert(!houji26AcquirePixels(UINTPTR_MAX-8,16,UINTPTR_MAX-8,8,64,16384,bounds));
        assert(bounds.cleaned.empty() && !bounds.barriers);
    }
    assert(!held);
    HoujiFrameStats26 timing;
    timing.record(10000, 200, 300, 50, 500, 1);
    timing.record(18333, 200, 300, 50, 750, 1);
    timing.record(35000, 100, 300, 50, 600, 1);
    timing.record(95000, 100, 500, 50, 700, 2);
    timing.record(126000, 100, 500, 50, 700, 2);
    assert(timing.copies == 5 && timing.interactiveUs == 56000);
    assert(timing.within[0] == 1 && timing.within[2] == 1 && timing.within[4] == 1);
    assert(timing.queueUs == 700 && timing.fenceUs == 1900 && timing.cacheUs == 250);
    assert(timing.writeUs == 3250 && timing.maxWriteUs == 750);
    timing.recordLayer(0, 1200, 2670, 1200, 2670);
    timing.recordLayer(0, 1200, 2670, 1199, 2670);
    assert(timing.stretchedLayers == 0);
    timing.recordLayer(2, 1200, 300, 1000, 300);
    assert(timing.stretchedLayers == 1 && timing.layer == 2 && timing.destWidth == 1000);
    puts("PASS: actual iOS26 present lease and CPU16K cache acquisition: fence ownership, WB/NC views, late Device/fault rejection, bounds, overflow and unlock; no GPU");
}
