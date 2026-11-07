#!/usr/bin/env python3
"""Exercise the production dispatch loop with explicit lock/scheduler peers."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
GPU = ROOT / 'kernel/overlay/iokit/Kernel/houji-ufs-root/Adreno740'
source = (GPU / 'NativeAdrenoPublish.cpp').read_text()

def function(signature):
    start = source.index(signature)
    return source[start:source.index('\n}', start) + 2]

unit = r'''
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstddef>
#include <cstring>
#include "NativeAdrenoABI.h"
using IOReturn=int;
constexpr int kIOReturnSuccess=0,kIOReturnTimeout=1,kIOReturnNotReady=2,kIOReturnBadArgument=3;
constexpr int THREAD_UNINT=0,NSEC_PER_USEC=1000;
#define THREAD_CONTINUE_NULL nullptr
using event_t=void*;
static bool held=false,armed=false,running=true,closeOnSleep=false,stopOnSleep=false;
static unsigned pending=0,polls=0,dispatches=0,sleeps=0,signals=0,pauseUs=0;
static unsigned imports=0;
static void *current_task(){return reinterpret_cast<void*>(0x1234);}
static int gateError=0,dispatchError=0;
static uint64_t clockUs=0,nativeAdrenoDispatchPerf[20][4],native_adreno_wait_outer[5];
static uint32_t nativeAdrenoHintIssued;
static const void *nativeAdrenoMutationOwner;
static uint64_t nativeAdrenoGateRetries[2];
static void (*duringSleep)();
static uint64_t now(void*){return clockUs;}
struct {void *lock=(void*)1;bool dying=false;struct {uint64_t(*now_us)(void*);void *cookie;} clock={now,nullptr};} g;
static bool native_adreno_board_running(){return running;}
static void IOLockLock(void*){assert(!held);held=true;}
static void IOLockUnlock(void*){assert(held);held=false;}
static int native_adreno_board_prepare_submit(){
 assert(held);++polls;
 if(gateError)return gateError;
 if(pending){--pending;return kIOReturnTimeout;}
 return 0;
}
static void nativeAdrenoSignalProgressLocked(){assert(held);++signals;}
static void assert_wait_timeout(event_t event,int kind,uint32_t us,int unit){
 assert(!held && !armed && event==&native_adreno_wait_outer && kind==THREAD_UNINT && unit==NSEC_PER_USEC);
 assert(us && us<=100);armed=true;pauseUs=us;
}
struct Core {
 int importBo(const NativeAdrenoAbiBoImport *request,void *task,NativeAdrenoAbiBoAllocReply *reply){
  assert(held && !pending && nativeAdrenoMutationOwner && task==current_task());
  assert(request->address==0x40000 && request->bytes==16384);
  ++imports;reply->handle=77;return dispatchError;
 }
 int dispatch(uint32_t,const void*,size_t,void *output,size_t,void*){
  assert(held);++dispatches;*static_cast<int*>(output)=42;return dispatchError;
 }
 uint64_t windowPeak(){assert(held);return 0;}
} core;
struct AGXDeviceUserClient {unsigned slot_=0;bool live_=true,kernel_=true;Core *core_=&core;char name_[1]{};} client;
struct NativeAdrenoClientRecord{uint32_t peak_mib;};
static NativeAdrenoClientRecord *clientRecord(const char*){return nullptr;}
struct NativeAdrenoDispatchClock {void lap(unsigned){}};
static void thread_block(void*){
 assert(!held && armed);++sleeps;clockUs+=pauseUs;armed=false;
 // An unrelated completion observer can acquire the mutex while we wait.
 IOLockLock(g.lock);nativeAdrenoSignalProgressLocked();IOLockUnlock(g.lock);
 if(duringSleep)duringSleep();
 if(closeOnSleep)client.live_=false;
 if(stopOnSleep)g.dying=true;
}
static void reset(){
 assert(!held && !armed && !nativeAdrenoMutationOwner);running=true;g.dying=false;g.clock.now_us=now;client.live_=true;
 duringSleep=nullptr;
 closeOnSleep=stopOnSleep=false;pending=polls=dispatches=sleeps=signals=pauseUs=0;
 clockUs=0;gateError=dispatchError=0;
 imports=0;
}
'''
unit += r'''
static void nativeAdrenoIssuerLock(unsigned){IOLockLock(g.lock);}
static void nativeAdrenoIssuerUnlock(){IOLockUnlock(g.lock);}
static void nativeAdrenoIssuerContext(unsigned,unsigned,const void*,size_t){}
struct {void subject(uint64_t){} void context(unsigned,unsigned,uint64_t){}} nativeAdrenoIssuerTrace;
'''
unit += function('static bool nativeAdrenoNonMutatingGpuMethod(')
unit += '\n' + function('static bool nativeAdrenoSubmitMethod(')
unit += '\n' + function('static IOReturn nativeAdrenoRunGuarded(AGXDeviceUserClient *connection')
unit += r'''
int main(){
 NativeAdrenoDispatchClock timing;int output=0;
 const unsigned mutation=HOUJI_IOS26?NativeAdrenoMethodBoMap:NativeAdrenoMethodBoAlloc;
 for(unsigned method:{mutation,4u,5u,11u,12u,13u,14u,17u}){
  reset();pending=2;output=0;
  assert(!nativeAdrenoRunGuarded(&client,method,nullptr,0,&output,sizeof output,timing));
  assert(polls==3 && sleeps==2 && dispatches==1 && output==42 && !held && !armed);
 }
 for(unsigned method:{1u,7u,8u,9u,10u,15u}){
  reset();pending=100;
  assert(!nativeAdrenoRunGuarded(&client,method,nullptr,0,&output,sizeof output,timing));
  assert(!polls && !sleeps && dispatches==1);
 }
 if(HOUJI_IOS26){
  reset();pending=100;gateError=9;
  assert(!nativeAdrenoRunGuarded(&client,NativeAdrenoMethodBoAlloc,nullptr,0,&output,sizeof output,timing));
  assert(!polls && !sleeps && dispatches==1 && pending==100 && !held);
 }
 reset();pending=100000;output=7;
 assert(nativeAdrenoRunGuarded(&client,mutation,nullptr,0,&output,sizeof output,timing)==kIOReturnTimeout);
 assert(clockUs==500000 && !dispatches && output==7 && !held && !armed);
 reset();gateError=9;output=7;
 assert(nativeAdrenoRunGuarded(&client,3,nullptr,0,&output,sizeof output,timing)==9);
 assert(!dispatches && !sleeps && output==7 && !held);
 reset();dispatchError=kIOReturnTimeout;
 assert(nativeAdrenoRunGuarded(&client,4,nullptr,0,&output,sizeof output,timing)==kIOReturnTimeout);
 assert(dispatches==1 && polls==1 && !sleeps); // a failed mutation is never retried
 for(bool stop:{false,true}){
  reset();pending=1;closeOnSleep=!stop;stopOnSleep=stop;
  assert(nativeAdrenoRunGuarded(&client,mutation,nullptr,0,&output,sizeof output,timing)==kIOReturnNotReady);
  assert(sleeps==1 && polls==1 && !dispatches && !held);
 }
 reset();running=false;
 assert(nativeAdrenoRunGuarded(&client,mutation,nullptr,0,&output,sizeof output,timing)==kIOReturnNotReady);
 assert(!polls && !dispatches && !held);
 reset();g.clock.now_us=nullptr;pending=1;
 assert(nativeAdrenoRunGuarded(&client,mutation,nullptr,0,&output,sizeof output,timing)==kIOReturnTimeout);
 assert(polls==1 && !sleeps && !dispatches && !held);
 // A mutation keeps its place across sleep. New work may not replenish the
 // GPU first; an unrelated bookkeeping query can still finish immediately.
 reset();pending=1;
 duringSleep=[](){
  assert(nativeAdrenoMutationOwner && !held);auto owner=nativeAdrenoMutationOwner;
  NativeAdrenoDispatchClock other;int result=0;g.clock.now_us=nullptr;
  unsigned before=polls,operations=dispatches;
  assert(nativeAdrenoRunGuarded(&client,17,nullptr,0,&result,sizeof result,other)==kIOReturnTimeout);
  assert(polls==before && dispatches==operations && owner==nativeAdrenoMutationOwner);
  assert(!nativeAdrenoRunGuarded(&client,8,nullptr,0,&result,sizeof result,other));
  assert(result==42 && dispatches==operations+1 && owner==nativeAdrenoMutationOwner);
  g.clock.now_us=now;
 };
 assert(!nativeAdrenoRunGuarded(&client,mutation,nullptr,0,&output,sizeof output,timing));
 assert(polls==2 && sleeps==1 && dispatches==2 && !nativeAdrenoMutationOwner && !held);
 // Host imports previously bypassed the gate entirely. Exercise the actual
 // import branch, including a completion observer during the unlocked wait.
 NativeAdrenoAbiBoImport request{0x40000,16384};NativeAdrenoAbiBoAllocReply reply{};
 auto import=[&](){return nativeAdrenoRunGuarded(&client,NativeAdrenoMethodBoImport,&request,sizeof request,&reply,sizeof reply,timing);};
 reset();pending=3;
 assert(!import() && reply.handle==77 && imports==1 && !dispatches && sleeps==3 && polls==4 && !held);
 reset();dispatchError=kIOReturnTimeout;reply.handle=123;
 assert(import()==kIOReturnTimeout && imports==1 && polls==1 && !sleeps && reply.handle==123);
 reset();pending=100000;reply.handle=123;
 assert(import()==kIOReturnTimeout && clockUs==500000 && !imports && !dispatches && reply.handle==123 && !held);
 for(bool stop:{false,true}){
  reset();pending=1;closeOnSleep=!stop;stopOnSleep=stop;reply.handle=123;
  assert(import()==kIOReturnNotReady && sleeps==1 && !imports && reply.handle==123 && !held);
 }
 reset();gateError=9;
 assert(import()==9 && !imports && !sleeps && !held);
 for(unsigned broken=0;broken<4;++broken){
  reset();
  assert(nativeAdrenoRunGuarded(&client,NativeAdrenoMethodBoImport,broken==0?nullptr:&request,
   broken==1?sizeof request-1:sizeof request,broken==2?nullptr:&reply,broken==3?sizeof reply-1:sizeof reply,timing)==kIOReturnBadArgument);
  assert(!polls && !imports && !dispatches && !held);
 }
 puts("PASS: actual BO/import/submit dispatch releases the issuer lock while pending; import preserves caller task, validation and reply-on-success; close/stop/deadline/error paths never mutate or retry a dispatched operation; no GPU/FPS claim");
}
'''
unit = '#include <initializer_list>\n' + unit
with tempfile.TemporaryDirectory(prefix='houji-dispatch-') as directory:
    cpp, binary = Path(directory) / 'dispatch.cpp', Path(directory) / 'dispatch'
    cpp.write_text(unit)
    for ios26 in (0, 1):
        subprocess.run(['clang++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        f'-DHOUJI_IOS26={ios26}', '-fsanitize=address,undefined', '-I', str(GPU),
                        str(cpp), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True, timeout=30)
