#!/usr/bin/env python3
"""Exercise the production mutex observers; no GPU, scheduling or FPS claim."""
from pathlib import Path
import importlib.util
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
GPU = ROOT / 'kernel/overlay/iokit/Kernel/houji-ufs-root/Adreno740'
source = (GPU / 'NativeAdrenoPublish.cpp').read_text()
helpers = source[source.index('static NativeAdrenoIssuerTrace nativeAdrenoIssuerTrace;'):
                 source.index('static uint64_t gHoujiPresentSync[14];')]
unit = r'''
#include <cassert>
#include <cstdio>
#include <cstring>
#include <atomic>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
#include "NativeAdrenoIssuerTrace.h"
#include "NativeAdrenoABI.h"
static std::mutex mutex;
static bool held=false;
static unsigned locks=0,unlocks=0,clockCalls=0;
static uint64_t now=0;
// Clock observation before an acquisition is also allowed.
static uint64_t observationClock(void*){++clockCalls;return ++now;}
static void IOLockLock(void*){mutex.lock();assert(!held);held=true;++locks;}
static bool IOLockTryLock(void*){
 if(!mutex.try_lock())return false;
 assert(!held);held=true;++locks;return true;
}
static void IOLockUnlock(void*){assert(held);held=false;++unlocks;mutex.unlock();}
struct Ring {
 uint32_t lastSubmitted(){assert(held);return 42;}
 uint32_t completedFence(){assert(held);return 41;}
} ring;
struct {void *lock=&mutex;Ring *ring=&::ring;struct {uint64_t(*now_us)(void*);void *cookie;} clock={observationClock,nullptr};} g;
''' + helpers + r'''
static std::string snapshot(const NativeAdrenoIssuerTrace &trace) {
 std::string text;
 trace.snapshot([&](const char *format,auto...args) {
  char line[1024];const int n=snprintf(line,sizeof line,format,args...);
  assert(n>0 && size_t(n)<sizeof line);text+=line;
 });
 return text;
}
static void verify(const std::string &text) {
 size_t at=0;
 while((at=text.find("HOUJI LOCK: ",at))!=text.npos) {
  unsigned long long seq,site,method,client,subject,request,acquired,released,issued,seen;
  assert(sscanf(text.c_str()+at,"HOUJI LOCK: seq=%llu site=%llu method=%llu client=%llu subject=%llu request=%llu acquired=%llu released=%llu issued=%llu seen=%llu",&seq,&site,&method,&client,&subject,&request,&acquired,&released,&issued,&seen)==10);
  assert(request<=acquired && acquired<=released && issued==42 && seen==41);
  assert(released-acquired>=1000 || acquired-request>=1000);
  ++at;
 }
}
int main() {
 // Disabled wrappers still acquire/release the original mutex, without clock
 // reads or records. A failed try-lock must not replace the active owner.
 nativeAdrenoIssuerLock(10);nativeAdrenoIssuerUnlock();
 assert(locks==1 && unlocks==1 && !clockCalls && !nativeAdrenoIssuerTrace.calls);
 nativeAdrenoIssuerTrace.enabled=true;
 nativeAdrenoIssuerLock(11);
 NativeAdrenoAbiWaitFence wait={37,1000000};
 nativeAdrenoIssuerContext(NativeAdrenoMethodWaitFence,2,&wait,sizeof wait);
 std::thread blocked([]{assert(!nativeAdrenoIssuerTryLock(99));});blocked.join();
 assert(nativeAdrenoIssuerTrace.active[3]==11 && nativeAdrenoIssuerTrace.active[6]==37);
 now+=3000;nativeAdrenoIssuerUnlock();
 auto text=snapshot(nativeAdrenoIssuerTrace);verify(text);
 assert(text.find("site=11 method=6 client=2 subject=37")!=text.npos);
 assert(nativeAdrenoIssuerTryLock(12));
 nativeAdrenoIssuerContext(NativeAdrenoMethodSubmitBatch,0,nullptr,0);
 nativeAdrenoIssuerTrace.subject(42);now+=2000;nativeAdrenoIssuerUnlock();
 assert(snapshot(nativeAdrenoIssuerTrace).find("site=12 method=17 client=0 subject=42")!=text.npos);
 assert(locks==unlocks);
 NativeAdrenoIssuerTrace trace;trace.enabled=true;
 trace.begin(100,110,123);trace.context(6,1,100);trace.end(100010,42,41);
 for(unsigned i=0;i<80;++i){trace.begin(200000+i*2000,200001+i*2000,22);trace.end(201100+i*2000,42,41);}
 text=snapshot(trace);verify(text);
 assert(text.find("site=123 method=6 client=1 subject=100")!=text.npos); // longest survives a lap
 trace.begin(20,10,1);trace.end(30,42,41);assert(trace.invalid==1);
 trace.begin(40,50,1);trace.end(49,42,41);assert(trace.invalid==2);
 NativeAdrenoIssuerTrace concurrent;concurrent.enabled=true;
 std::mutex writer;std::atomic<unsigned> done{0};std::vector<std::thread> threads;
 for(unsigned id=0;id<4;++id)threads.emplace_back([&,id]{
  for(unsigned i=0;i<5000;++i){
   std::lock_guard<std::mutex> guard(writer);
   const uint64_t base=(id*5000+i)*10000ull;
   concurrent.begin(base,base+2000,id);concurrent.context(6,id,base);
   concurrent.end(base+4000,42,41);
  }
  ++done;
 });
 do{verify(snapshot(concurrent));}while(done.load()!=4);
 for(auto &thread:threads)thread.join();
 assert(concurrent.calls==20000 && concurrent.records==20000 && !concurrent.invalid);
}
'''
with tempfile.TemporaryDirectory(prefix='houji-issuer-trace-') as directory:
    path = Path(directory)
    cpp, binary = path / 'issuer.cpp', path / 'issuer'
    cpp.write_text(unit)
    subprocess.run(['clang++', '-std=c++17', '-O1', '-g',
                    '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                    '-I', str(GPU), str(cpp), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=60)

    # Apply only the two iOS26 hunks to exercise the actual quiet watchdog.
    reused = ROOT / 'kernel/ios26/patches/reused-drivers.patch'
    base = Path('iokit/Kernel/houji-ufs-root')
    publish = base / 'Adreno740/NativeAdrenoPublish.cpp'
    live = base / 'HoujiGpuNativeLive.inc'
    for name in (publish, live):
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / 'kernel/overlay' / name).read_bytes())
    subprocess.run(['git', 'apply', f'--include={publish}', f'--include={live}', str(reused)],
                   cwd=path, check=True)
    upgraded = (path / publish).read_text()
    owner_helpers = upgraded[upgraded.index('static NativeAdrenoIssuerTrace nativeAdrenoIssuerTrace;'):
                             upgraded.index('static uint64_t gHoujiPresentSync[14];')]
    monitor = (path / live).read_text()
    begin = monitor.index('   const uint64_t owner=native_adreno_provider_gate_owner();')
    end = monitor.index('  }else {busy_since=0;', begin)
    watch = r'''
static uint64_t watchTime, busy_owner, busy_since;
static unsigned watchdogPanics;
static uint64_t houjiSubmitNow(void*) { return watchTime; }
static uint64_t native_adreno_provider_progress_token() { return (1U << 16) | 6; }
static void panic(const char*, unsigned site, unsigned selector, unsigned inflight) {
 assert(site == 22 && selector == 6 && inflight == 1);
 ++watchdogPanics;
}
static void observeBusy(uint64_t timestamp) {
 watchTime = timestamp;
''' + monitor[begin:end] + r'''
}
static void checkWatchdog() {
 for (unsigned i = 1; i < 5; ++i) {
  nativeAdrenoIssuerLock(22);
  observeBusy(i * 11000000ULL);
  nativeAdrenoIssuerUnlock();
  assert(uint32_t(native_adreno_provider_gate_owner()) == 0 && watchdogPanics == 0);
 }
 nativeAdrenoIssuerLock(22);
 observeBusy(55000000);
 observeBusy(64999999);
 assert(watchdogPanics == 0);
 observeBusy(65000001);
 assert(watchdogPanics == 1);
 nativeAdrenoIssuerUnlock();
 observeBusy(66000000);
 assert(busy_since == 0);
}
'''
    upgraded_unit = unit.replace(helpers, owner_helpers, 1).replace('int main() {', watch + 'int main() {', 1)
    upgraded_unit = upgraded_unit.replace(' assert(locks==unlocks);',
        ' assert(locks==unlocks);\n nativeAdrenoIssuerTrace.enabled=false;\n checkWatchdog();', 1)
    cpp.write_text(upgraded_unit)
    subprocess.run(['clang++', '-std=c++17', '-O1', '-g', '-fsanitize=address,undefined',
                    '-I', str(GPU), str(cpp), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=60)
    print('PASS: iOS26 gate watchdog distinguishes successive acquisitions from one held owner')

spec = importlib.util.spec_from_file_location('issuer', ROOT/'tools/analyze-gpu-issuer.py')
parser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parser)
line = 'HOUJI LOCK: seq=1 site=2 method=6 client=0 subject=37 request=1000 acquired=4000 released=5000 issued=42 seen=41'
stage = {'slow': [{'sequence': 100, 'commit_uptime_s': .003, 'callback_us': 1500},
                  {'sequence': 101, 'commit_uptime_s': .010, 'callback_us': 2000}]}
data = parser.analyze(line+'\n'+line+'\nHOUJI LOCK: seq=2 site=', 'first\nlock();\n', stage)
assert len(data['records']) == 1 and data['incomplete_rows'] == 1
assert data['records'][0]['wait_us'] == 3000 and data['records'][0]['held_us'] == 1000
assert data['records'][0]['source_line'] == 'lock();'
assert data['command_overlaps'][0]['holds'] == [{'issuer_sequence': 1, 'held_overlap_us': 500}]
assert data['command_overlaps'][1]['holds'] == []
assert parser.analyze('unrelated log')['status'] == 'NO_ISSUER_RECORDS'
for bad in [line.replace('acquired=4000', 'acquired=900'), line+'\n'+line.replace('site=2', 'site=3')]:
    try:
        parser.analyze(bad)
    except ValueError:
        pass
    else:
        raise AssertionError('invalid or mixed-boot issuer record accepted')
print('PASS: issuer observations preserve mutex calls; off, try-lock contention, fence identity, longest retention, invalid clocks, concurrent snapshots and interval parser; no GPU')
