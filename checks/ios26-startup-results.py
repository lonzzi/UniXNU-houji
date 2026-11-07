#!/usr/bin/env python3
"""Exercise the actual bounded startup writer with recording host callbacks."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / 'kernel/ios26/overlay/iokit/Kernel/arm/HoujiPlatform26.cpp').read_text()
start = source.index('/* Optional, finite PID1 bring-up results.')
end = source.index('extern "C" uint64_t houji_cpu_idle_mach', start)
unit = r'''
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>
using thread_t = void *;
using wait_result_t = int;
using IOReturn = int;
constexpr thread_t THREAD_NULL = nullptr;
constexpr int KERN_SUCCESS = 0;
using entry_t = void (*)(void *, wait_result_t);
static int enabled, native_root, recovery, detail, services, panic_state, thread_error;
static bool interruptible = true;
static unsigned thread_calls, log_calls, sleeps, service_reads, gpu_snapshots;
static entry_t queued;
static std::vector<std::string> writes;
static bool PE_parse_boot_argn(const char *name, void *value, int size) {
    assert(size == sizeof(int));
    int result = !strcmp(name,"houji_pid1_startup") ? enabled :
        !strcmp(name,"houji_native_root") ? native_root :
        !strcmp(name,"houji_pid1_detail") ? detail :
        !strcmp(name,"houji_pid1_services") ? services : recovery;
    memcpy(value, &result, sizeof result); return true;
}
static unsigned panic_active() { return panic_state; }
static bool ml_get_interrupts_enabled() { return interruptible; }
static void IOLog(const char *, ...) { ++log_calls; }
extern "C" void houji_ufs_checkpoint(const char *text, unsigned stop) {
    assert(stop == 0 && !panic_state && interruptible);
    writes.emplace_back(text); assert(writes.back().size() < 768);
}
static int kernel_thread_start(entry_t entry, void *, thread_t *thread) {
    ++thread_calls; if(thread_error) return thread_error;
    queued = entry; *thread = (void *)1; return 0;
}
static void thread_deallocate(thread_t thread) { assert(thread == (void *)1); }
static void IOSleep(unsigned milliseconds) { assert(milliseconds == 30000); ++sleeps; }
extern "C" uint32_t houji26_process_presence(const char *const *names, unsigned count) {
    assert(count == 5 && !strcmp(names[1], "backboardd") && !strcmp(names[4], "SpringBoard"));
    ++service_reads;
    return 0x16;
}
extern "C" IOReturn native_adreno_provider_desktop_stats(uint64_t stats[7]) {
    stats[0] = 12; stats[4] = 110;
    return 0;
}
extern "C" void native_adreno_provider_snapshot(void) { ++gpu_snapshots; }
'''
unit += source[start:end]
unit += r'''
static void reset() {
    enabled = native_root = recovery = detail = 1; panic_state = thread_error = 0;
    services = 0; sleeps = service_reads = gpu_snapshots = 0;
    interruptible = true; thread_calls = log_calls = bootResults = bootMainSeen = bootTerminalResult = 0;
    queued = nullptr; writes.clear();
}
int main() {
    for(unsigned gate=0; gate<5; ++gate) {
        reset();
        if(gate==0) enabled=0;
        if(gate==1) native_root=0;
        if(gate==2) recovery=0;
        if(gate==3) panic_state=1;
        if(gate==4) interruptible=false;
        houji26_boot_exec("/sbin/launchd",8,0);
        houji26_boot_reboot("launchd",8,"reason");
        houji26_boot_exit(6,1,6,"dyld failure",12);
        houji26_boot_main();
        assert(writes.empty() && !log_calls && !thread_calls);
    }
    reset();
    houji26_boot_exec("/Applications/arbitrary",0,1);
    houji26_boot_exec("/sbin/launchd",0,-1);
    houji26_boot_exec("/sbin/launchd",0,9);
    assert(writes.empty());
    for(const char *path:{"/sbin/launchd","/redwood/sbin/init","/sbin/launchd.ios"}) {
        houji26_boot_exec(path,0,1);houji26_boot_exec(path,0,0);
    }
    assert(writes.size()==6);
    houji26_boot_main();
    assert(thread_calls==1 && queued && writes.size()==6);
    queued(nullptr,0);
    assert(writes.size()==7 && writes.back().find("stock-launchd-main")!=std::string::npos);
    houji26_boot_main();houji26_boot_exec("/sbin/launchd",0,1);
    assert(thread_calls==1 && writes.size()==7);
    std::string large(4096,'x');
    houji26_boot_reboot("launchd",8,large.c_str());
    for(unsigned i=0;i<20;++i) houji26_boot_reboot("launchd",8,nullptr);
    assert(writes.size()==8 && log_calls==8);
    assert(writes.back().find("result=8 main=1")!=std::string::npos);
    reset();houji26_boot_main();panic_state=1;queued(nullptr,0);
    assert(writes.empty());
    reset();thread_error=1;houji26_boot_main();assert(!queued && writes.empty());
    reset();
    for(int phase=2;phase<=8;++phase) houji26_boot_exec("/sbin/launchd",phase,phase);
    assert(writes.size()==7);
    assert(writes[0].find("pid1-clone-ready")!=std::string::npos);
    assert(writes[1].find("pid1-image-activated")!=std::string::npos);
    assert(writes[2].find("pid1-signature-checked")!=std::string::npos);
    assert(writes[3].find("pid1-exec-ready")!=std::string::npos);
    assert(writes[4].find("pid1-vnode-header-ready")!=std::string::npos);
    assert(writes[5].find("pid1-macho-load-begin")!=std::string::npos);
    assert(writes[6].find("pid1-macho-load-end")!=std::string::npos);
    reset();uint32_t header[8]={0xfeedfacf,0x100000c,0x80000002,2,9,976,1,0};
    houji26_boot_header("/sbin/launchd",header,31,0,33680);
    houji26_boot_header("/sbin/launchd",header,32,33,33680);
    assert(writes.empty());
    houji26_boot_header("/sbin/launchd",header,16384,0,33680);
    assert(writes.size()==1 && writes[0].find("header=feedfacf,0100000c,80000002,00000002")!=std::string::npos);
    assert(writes[0].find("read=16384 resid=0 file-bytes=33680")!=std::string::npos);
    reset();
    for(int phase=1;phase<=8;++phase) houji26_boot_exec("/sbin/launchd",0,phase);
    assert(writes.size()==7);
    const char bounded[3]={'L','i','b'};
    houji26_boot_exit(6,1,6,bounded,sizeof(bounded));
    assert(writes.size()==8 && writes.back().find("namespace=6 subcode=1 description=Lib")!=std::string::npos);
    houji26_boot_exit(6,2,6,"later reason",12);
    assert(writes.size()==8 && writes.back().find("description=Lib")!=std::string::npos);
    reset();detail=0;
    for(int phase=2;phase<=8;phase++)houji26_boot_exec("/sbin/launchd",0,phase);
    houji26_boot_header("/sbin/launchd",header,16384,0,33680);
    assert(writes.empty());
    for(const char *path:{"/sbin/launchd","/redwood/sbin/init","/sbin/launchd.ios"}){
        houji26_boot_exec(path,0,1);houji26_boot_exec(path,0,0);
    }
    houji26_boot_main();queued(nullptr,0);assert(writes.size()==7);
    houji26_boot_exit(6,1,6,"Library not loaded",18);assert(writes.size()==8);
    houji26_boot_gpu_end(0,180000000);
    assert(writes.size()==8 && !sleeps && !service_reads && !gpu_snapshots);
    reset();services=1;
    for (const char *path : {"/sbin/launchd", "/redwood/sbin/init", "/sbin/launchd.ios"}) {
        houji26_boot_exec(path,0,1);houji26_boot_exec(path,0,0);
    }
    houji26_boot_main();queued(nullptr,0);
    assert(writes.size()==8 && sleeps==1 && service_reads==1 && gpu_snapshots==1);
    assert(writes.back().find("notifyd=0 backboardd=1 runningboardd=1 lsd=0 SpringBoard=1")!=std::string::npos);
    assert(writes.back().find("gpu-query=0 backboard-submits=12 submit-errors=0 completed-fence=110")!=std::string::npos);
    houji26_boot_exit(6,1,6,"later exit",10);
    assert(writes.size()==9 && writes.back().find("pid1-critical-exit")!=std::string::npos);
    houji26_boot_gpu_end(15,180000000);
    assert(writes.size()==10 && gpu_snapshots==2);
    assert(writes.back().find("gpu-runtime-end object=mode=3 result=15 main=1 elapsed-us=180000000")!=std::string::npos);
    panic_state=1;houji26_boot_gpu_end(0,180000000);
    assert(writes.size()==10 && gpu_snapshots==2);
}
'''
with tempfile.TemporaryDirectory(prefix='houji26-startup-results-') as name:
    directory = Path(name)
    cpp, binary = directory / 'check.cpp', directory / 'check'
    cpp.write_text(unit)
    subprocess.run(['clang++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                    str(cpp), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
print('PASS: actual startup writer keeps the default eight records, allows opt-in service and GPU exit records, and retains the first terminal reason; host peers, no device or GPU')
