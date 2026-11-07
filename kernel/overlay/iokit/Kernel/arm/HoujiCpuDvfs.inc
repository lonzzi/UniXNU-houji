/* SPDX-License-Identifier: BSD-2-Clause-Patent
 * SM8650 board EPSS governor. Register facts: MiCode qcom-cpufreq-hw;
 * policy: UniXNU-sheng CpuFreqProbe (08cde20e). No raw voltage/PLL writes.
 * Four domains and caps are THIS phone's FDT + recovery cpufreq inventory.
 */
#include <IOKit/IOMemoryDescriptor.h>
#include "HoujiDvfsPolicy.h"
extern "C" uint64_t houji_cpu_idle_mach(int);
extern "C" uint64_t houji_gpu_submit_activity(void);
static uint64_t houjiTouchTime;
static uint64_t houjiTouchBurstTime;
extern "C" void houji_dvfs_touch(void) {
 const uint64_t now=mach_absolute_time();
 uint64_t previous=__atomic_load_n(&houjiTouchTime,__ATOMIC_RELAXED);
 while(now>previous && !__atomic_compare_exchange_n(&houjiTouchTime,&previous,now,false,__ATOMIC_RELAXED,__ATOMIC_RELAXED)){}
 if(now<=previous)return;
 uint64_t gapNs=0;if(previous)absolutetime_to_nanoseconds(now-previous,&gapNs);
 // Record the start of a contact burst at the input source, not at a slower
 // governor poll. A held/moving finger must not renew the short idle prewarm.
 if(!previous || gapNs>=80000000ULL){
  uint64_t old=__atomic_load_n(&houjiTouchBurstTime,__ATOMIC_RELAXED);
  while(now>old && !__atomic_compare_exchange_n(&houjiTouchBurstTime,&old,now,false,__ATOMIC_RELAXED,__ATOMIC_RELAXED)){}
 }
}
extern "C" uint64_t houji_dvfs_touch_time(void) {
 return __atomic_load_n(&houjiTouchTime,__ATOMIC_RELAXED);
}
extern "C" uint64_t houji_dvfs_touch_burst_time(void) {
 return __atomic_load_n(&houjiTouchBurstTime,__ATOMIC_RELAXED);
}
namespace HoujiCpuDvfs {
constexpr unsigned domains=4,rows=40;
constexpr unsigned cpuDomain[8]={0,0,3,3,3,1,1,2},counts[4]={2,2,1,3};
// Recovery exposes X4's top four EPSS rows in scaling_boost_frequencies,
// separately from scaling_available_frequencies. boost=1 and cpuinfo_max_freq
// confirm 3302400 kHz; no frequency or voltage is synthesized here.
constexpr uint32_t caps[4]={2265600,2956800,3302400,3148800};
constexpr uint32_t minima[4]={364800,499200,480000,499200};
struct Domain {
 IOMemoryMap *map;uintptr_t va;uint32_t khz[rows];uint8_t index[rows];
 unsigned n,cur,low,load,changes,readback,bad,initial;uint32_t state;
};
static Domain d[domains];
static bool started;
static unsigned enabled;
struct History {
 uint64_t upMs,us,boostUs,frameUs,maxTickUs;
 uint32_t core[8],request[4],stateMin[4],stateMax[4],throttled[4],changes[4];
};
static History history[48];
static unsigned historyCount;
// Keep the latest complete interval separately from the bounded boot history.
// Late interaction/thermal samples must survive message-ring wrap and the
// first 240 seconds. One writer publishes atomically; snapshots never read HW.
struct LatestWindow {uint32_t sequence;uint64_t values[33];};
static LatestWindow latest;
static uint32_t read(Domain&w,unsigned off){return *(volatile uint32_t*)(w.va+off);}
static bool init(unsigned id){
 auto&w=d[id];auto*m=IODeviceMemory::withRange(0x17d91000ULL+id*0x1000,0x1000);
 w.map=m?m->map(kIOMapAnywhere|kIOMapInhibitCache):nullptr;if(m)m->release();
 w.va=w.map?w.map->getVirtualAddress():0;
 if(!w.va || !(read(w,0)&1))return false;
 uint32_t prev=0;
 for(unsigned i=0;i<rows;i++){
  uint32_t raw=read(w,0x100+i*4),src=raw>>30,khz=(raw&255)*19200;
  if(!src || khz<minima[id] || khz>4000000)return false;
  if(i && khz==prev)break;
  if(i && khz<prev)return false;prev=khz;
  if(khz>caps[id])continue;
  w.khz[w.n]=khz;w.index[w.n++]=i;
 }
 if(w.n<2 || w.khz[0]!=minima[id] || w.khz[w.n-1]!=caps[id])return false;
 w.initial=read(w,0x320);if(w.initial>=rows)return false;w.cur=w.n-1;
 for(unsigned i=0;i<w.n;i++)if(w.index[i]==w.initial)w.cur=i;
 IOLog("HOUJI CPU DVFS INIT: domain=%u cpus=%u rows=%u cap-kHz=%u initial-index=%u initial-request-kHz=%u control=%x; factory LUT, EPSS voltage control preserved\n",id,counts[id],w.n,caps[id],w.initial,(read(w,0x100+w.initial*4)&255)*19200,read(w,0xb0));
 return true;
}
static bool vote(unsigned id,unsigned at){
 auto&w=d[id];if(at>=w.n || !(read(w,0)&1))return false;
 unsigned n=(read(w,0xb0)&1)?counts[id]:1;
 for(unsigned c=0;c<n;c++)*(volatile uint32_t*)(w.va+0x320+c*4)=w.index[at];
 __asm__ volatile("dsb sy" ::: "memory");
 for(unsigned c=0;c<n;c++)if(read(w,0x320+c*4)!=w.index[at])return false;
 w.cur=at;++w.changes;return true;
}
static void worker(void*,wait_result_t){
 thread_set_thread_name(current_thread(),"houji-cpu-dvfs");
 uint64_t last=mach_absolute_time(),idle[8]={},report=last,boost=0,frameTicks=0,frameUntil=0;
 uint64_t submits=houji_gpu_submit_activity(),sumUs=0,boostUs=0,frameUs=0,maxTickUs=0;
 uint64_t coreUs[8]={},requestUs[4]={};
 unsigned recent=0,sequence=0,quiet[4]={},stateMin[4]={UINT32_MAX,UINT32_MAX,UINT32_MAX,UINT32_MAX},stateMax[4]={},throttled[4]={},changes[4]={};
 nanoseconds_to_absolutetime(1000000000,&boost);
 nanoseconds_to_absolutetime(300000000,&frameTicks);
 for(unsigned c=0;c<8;c++)idle[c]=houji_cpu_idle_mach(c);
 for(;;){
  IOSleep(20);uint64_t now=mach_absolute_time(),dt=now-last;last=now;
  if(!dt)continue;unsigned busy[4]={};uint64_t dtNs=0;absolutetime_to_nanoseconds(dt,&dtNs);uint64_t us=dtNs/1000;
  sumUs+=us;if(us>maxTickUs)maxTickUs=us;
  uint64_t nextSubmits=houji_gpu_submit_activity();recent=((recent<<1)|(nextSubmits!=submits))&15;submits=nextSubmits;
  if(__builtin_popcount(recent)>=2)frameUntil=now+frameTicks;
  bool framing=now<frameUntil;
  for(unsigned c=0;c<8;c++){
   uint64_t v=houji_cpu_idle_mach(c),old=idle[c];idle[c]=v;
   if(v==UINT64_MAX || old==UINT64_MAX || v<old)continue;
   uint64_t di=v-old;unsigned b=di>=dt?0:(unsigned)((dt-di)*1000/dt);
   coreUs[c]+=uint64_t(b)*us;
   if(b>busy[cpuDomain[c]])busy[cpuDomain[c]]=b;
  }
  uint64_t touch=houji_dvfs_touch_time();bool boosted=touch && now>=touch && now-touch<boost;
  if(boosted)boostUs+=us;if(framing)frameUs+=us;
  for(unsigned id=0;id<domains;id++){
   auto&w=d[id];if(w.bad)continue;
   w.load=busy[id];w.state=read(w,0x20);uint32_t state=(w.state&255)*19200;
   requestUs[id]+=uint64_t(w.khz[w.cur])*us;
   if(state<stateMin[id])stateMin[id]=state;if(state>stateMax[id])stateMax[id]=state;
   if(w.load>=500 && state && uint64_t(state)*5<uint64_t(w.khz[w.cur])*4)++throttled[id];
   unsigned floor=houjiDvfsInteractiveFloor(w.khz,w.n,w.load,id!=0,boosted,framing,&quiet[id]);
   unsigned at=houjiDvfsChoose(w.khz,w.n,w.cur,w.load,floor,&w.low);
   unsigned thermal=houji_thermal_cpu_cap(id);if(!thermal)thermal=1;
   if(at>=thermal){at=thermal-1;w.low=0;}
   if(at!=w.cur && !vote(id,at)){w.bad=1;IOLog("HOUJI CPU DVFS FAIL: domain=%u request/readback mismatch; domain writes disabled\n",id);}
   w.readback=read(w,0x320);w.state=read(w,0x20);
  }
  uint64_t ns=0;absolutetime_to_nanoseconds(now-report,&ns);
  if(ns>=5000000000ULL){report=now;
   if(sumUs){
    uint64_t upNs=0;absolutetime_to_nanoseconds(now,&upNs);
    uint64_t values[33]={upNs/1000000,sumUs,boostUs,frameUs,maxTickUs};
    for(unsigned c=0;c<8;c++)values[5+c]=coreUs[c]/sumUs;
    for(unsigned id=0;id<domains;id++){
     values[13+id]=requestUs[id]/sumUs;values[17+id]=stateMin[id];values[21+id]=stateMax[id];
     values[25+id]=throttled[id];values[29+id]=d[id].changes-changes[id];
    }
    __atomic_store_n(&latest.sequence,0u,__ATOMIC_RELEASE);
    __atomic_thread_fence(__ATOMIC_RELEASE);
    for(unsigned i=0;i<33;i++)__atomic_store_n(&latest.values[i],values[i],__ATOMIC_RELAXED);
    if(!++sequence)++sequence;
    __atomic_store_n(&latest.sequence,sequence,__ATOMIC_RELEASE);
   }
   unsigned count=__atomic_load_n(&historyCount,__ATOMIC_RELAXED);
   if(count<48 && sumUs){auto&h=history[count];uint64_t upNs=0;absolutetime_to_nanoseconds(now,&upNs);
    h.upMs=upNs/1000000;h.us=sumUs;h.boostUs=boostUs;h.frameUs=frameUs;h.maxTickUs=maxTickUs;
    for(unsigned c=0;c<8;c++)h.core[c]=(uint32_t)(coreUs[c]/sumUs);
    for(unsigned id=0;id<domains;id++){auto&w=d[id];h.request[id]=(uint32_t)(requestUs[id]/sumUs);h.stateMin[id]=stateMin[id];h.stateMax[id]=stateMax[id];h.throttled[id]=throttled[id];h.changes[id]=w.changes-changes[id];}
    __atomic_store_n(&historyCount,count+1,__ATOMIC_RELEASE);
   }
   sumUs=boostUs=frameUs=maxTickUs=0;for(auto&c:coreUs)c=0;
   for(unsigned id=0;id<domains;id++){requestUs[id]=0;stateMin[id]=UINT32_MAX;stateMax[id]=throttled[id]=0;changes[id]=d[id].changes;}
#if !HOUJI_IOS26
   for(unsigned id=0;id<domains;id++){auto&w=d[id];IOLog("HOUJI CPU DVFS: d=%u req-kHz=%u read-index=%u state-kHz=%u load-permille=%u changes=%u bad=%u touch-boost=%u; domain-state hardware sample, not cycle measurement\n",id,w.khz[w.cur],w.readback,(w.state&255)*19200,w.load,w.changes,w.bad,boosted);}
#endif
  }
 }
}
static void start(){
 if(started)return;started=true;PE_parse_boot_argn("houji_dvfs",&enabled,sizeof(enabled));
 if(!(enabled&1))return;
 for(unsigned id=0;id<domains;id++)if(!init(id)){IOLog("HOUJI CPU DVFS: domain=%u factory validation refused; no frequency writes\n",id);return;}
 for(unsigned id=0;id<domains;id++)houji_thermal_cpu_levels(id,d[id].n);
 houji_thermal_start();
 for(unsigned id=0;id<domains;id++)if(!vote(id,0)){IOLog("HOUJI CPU DVFS: initial readback refused domain=%u\n",id);return;}
 thread_t t;if(kernel_thread_start(worker,nullptr,&t)==KERN_SUCCESS)thread_deallocate(t);
}
}
extern "C" unsigned houji_cpu_dvfs_snapshot(char *out,unsigned capacity){
 if(!out || !capacity)return 0;out[0]=0;unsigned used=0;
 auto append=[&](const char *format,auto... args){
  if(used>=capacity-1)return;
  int n=snprintf(out+used,capacity-used,format,args...);
  if(n>0 && (unsigned)n<capacity-used)used+=(unsigned)n;
  else out[used]=0; // never leave a partial diagnostic row
 };
 auto &s=HoujiCpuDvfs::latest;uint64_t v[33];
 uint32_t sequence=__atomic_load_n(&s.sequence,__ATOMIC_ACQUIRE);
 if(!sequence){append("%s","HOUJI CPU SNAPSHOT: pending\n");return used;}
 for(unsigned i=0;i<33;i++)v[i]=__atomic_load_n(&s.values[i],__ATOMIC_RELAXED);
 __atomic_thread_fence(__ATOMIC_ACQUIRE);
 if(sequence!=__atomic_load_n(&s.sequence,__ATOMIC_ACQUIRE)){
  append("%s","HOUJI CPU SNAPSHOT: raced; interval omitted\n");return used;
 }
 append("HOUJI CPU SNAPSHOT: n=%u up-ms=%llu span-us=%llu touch-us=%llu frame-us=%llu max-tick-us=%llu; last complete interval, not power measurement\n",sequence,v[0],v[1],v[2],v[3],v[4]);
 append("HOUJI CPU LOAD: core-permille=%llu,%llu,%llu,%llu,%llu,%llu,%llu,%llu\n",v[5],v[6],v[7],v[8],v[9],v[10],v[11],v[12]);
 for(unsigned id=0;id<4;id++)append("HOUJI CPU DOMAIN: id=%u mean-request-kHz=%llu state-min=%llu state-max=%llu throttle-samples=%llu changes=%llu\n",id,v[13+id],v[17+id],v[21+id],v[25+id],v[29+id]);
 return used;
}
extern "C" void houji_cpu_dvfs_history_report(void){
 using namespace HoujiCpuDvfs;
 unsigned count=__atomic_load_n(&historyCount,__ATOMIC_ACQUIRE);if(count>48)count=48;
 for(unsigned i=0;i<count;i++){const auto&h=history[i];
  IOLog("HOUJI CPU WINDOW: up-ms=%llu span-ms=%llu core-permille=%u,%u,%u,%u,%u,%u,%u,%u mean-req-kHz=%u,%u,%u,%u state-min=%u,%u,%u,%u state-max=%u,%u,%u,%u throttle=%u,%u,%u,%u changes=%u,%u,%u,%u touch-ms=%llu frame-ms=%llu max-tick-us=%llu\n",h.upMs,h.us/1000,h.core[0],h.core[1],h.core[2],h.core[3],h.core[4],h.core[5],h.core[6],h.core[7],h.request[0],h.request[1],h.request[2],h.request[3],h.stateMin[0],h.stateMin[1],h.stateMin[2],h.stateMin[3],h.stateMax[0],h.stateMax[1],h.stateMax[2],h.stateMax[3],h.throttled[0],h.throttled[1],h.throttled[2],h.throttled[3],h.changes[0],h.changes[1],h.changes[2],h.changes[3],h.boostUs/1000,h.frameUs/1000,h.maxTickUs);
 }
}
