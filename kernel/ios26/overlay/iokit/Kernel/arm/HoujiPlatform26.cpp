/* SPDX-License-Identifier: BSD-2-Clause-Patent
 * Houji hardware policies on the reference's XNU12377 platform. The platform
 * owns CPU registration, PSCI, GIC and IOSurface; this file retains SM8650
 * EPSS/TSENS and translates the existing touch producer to the iOS26 input ABI.
 */
#include <IOKit/IOLib.h>
#include <IOKit/IODeviceTreeSupport.h>
#include <IOKit/IOPlatformExpert.h>
#include <IOKit/IODeviceMemory.h>
#include <IOKit/IOMemoryDescriptor.h>
#include <kern/thread.h>
#include <kern/processor.h>
#include <kern/clock.h>
#include <kern/debug.h>
#include <machine/machine_routines.h>
#include <pexpert/pexpert.h>
#include <sys/proc.h>
extern "C" {
#include <sys/redwood_input.h>
}
#include "HoujiCpuPolicy.h"
#include "HoujiThermal.inc"
#include "HoujiCpuDvfs.inc"

extern "C" uint64_t hakios_cpu_idle_mach(int cpu);
extern "C" uint32_t houji26_running_mask(void);
extern "C" void houji26_irq_counts(unsigned cpu, uint64_t *timer, uint64_t *ipi);
extern "C" processor_t thread_bind(processor_t processor);
extern uint32_t processor_avail_count;

/* Optional, finite PID1 bring-up results. No periodic or per-frame tracing. */
extern "C" void houji_ufs_checkpoint(const char *, unsigned);
extern "C" uint32_t houji26_process_presence(const char *const *, unsigned);
extern "C" IOReturn native_adreno_provider_desktop_stats(uint64_t[7]);
static uint32_t bootResults, bootMainSeen, bootTerminalResult;
static bool bootResultsEnabled(void) {
    int enabled = 0, native = 0, recovery = 0;
    PE_parse_boot_argn("houji_pid1_startup", &enabled, sizeof(enabled));
    PE_parse_boot_argn("houji_native_root", &native, sizeof(native));
    PE_parse_boot_argn("houji_return_recovery", &recovery, sizeof(recovery));
    return enabled == 1 && native == 1 && recovery == 1 && !panic_active() && ml_get_interrupts_enabled();
}
static bool bootDetailsEnabled(void) {
    int detail = 0;
    PE_parse_boot_argn("houji_pid1_detail", &detail, sizeof(detail));
    return detail == 1;
}
static void saveBootResult(const char *kind, const char *object, int result, const char *detail) {
    char stage[768];
    snprintf(stage, sizeof(stage), "HOUJI26 STARTUP RESULT: %s object=%.128s result=%d main=%u %.512s",
        kind, object ? object : "", result, __atomic_load_n(&bootMainSeen, __ATOMIC_ACQUIRE), detail ? detail : "");
    IOLog("%s\n", stage);
    houji_ufs_checkpoint(stage, 0);
}
static void bootResult(const char *kind, const char *object, int result, const char *detail, bool terminal = false) {
    if (!bootResultsEnabled()) return;
    if (terminal) {
        if (__atomic_exchange_n(&bootTerminalResult, 1U, __ATOMIC_ACQ_REL)) return;
    } else if (__atomic_fetch_add(&bootResults, 1U, __ATOMIC_ACQ_REL) >= 7) return;
    saveBootResult(kind, object, result, detail);
}
extern "C" void houji26_boot_exec(const char *path, int result, int phase) {
    if (!path || __atomic_load_n(&bootMainSeen, __ATOMIC_ACQUIRE)) return;
    if (strcmp(path, "/sbin/launchd") && strcmp(path, "/redwood/sbin/init") &&
        strcmp(path, "/sbin/launchd.ios")) return;
    static const char *const kinds[] = {"pid1-exec-return", "pid1-exec-begin", "pid1-clone-ready",
        "pid1-image-activated", "pid1-signature-checked", "pid1-exec-ready",
        "pid1-vnode-header-ready", "pid1-macho-load-begin", "pid1-macho-load-end"};
    if (phase < 0 || static_cast<unsigned>(phase) >= sizeof(kinds) / sizeof(kinds[0])) return;
    if (phase >= 2 && !bootDetailsEnabled()) return;
    bootResult(kinds[phase], path, result, "");
}
extern "C" void houji26_boot_header(const char *path, const void *data, unsigned bytes, int resid, uint64_t file_bytes) {
    if (!bootResultsEnabled() || !bootDetailsEnabled() || !path || !data || bytes < 32 || resid < 0 || static_cast<unsigned>(resid) > bytes ||
        __atomic_load_n(&bootMainSeen, __ATOMIC_ACQUIRE)) return;
    if (strcmp(path, "/sbin/launchd") && strcmp(path, "/redwood/sbin/init") && strcmp(path, "/sbin/launchd.ios")) return;
    uint32_t words[8]; memcpy(words, data, sizeof(words));
    char detail[256];
    snprintf(detail, sizeof(detail), "header=%08x,%08x,%08x,%08x,%08x,%08x,%08x,%08x read=%u resid=%d file-bytes=%llu",
        words[0],words[1],words[2],words[3],words[4],words[5],words[6],words[7],bytes,resid,(unsigned long long)file_bytes);
    bootResult("pid1-vnode-header-ready", path, 0, detail);
}
static void bootMainWorker(void *, wait_result_t) {
    bootResult("stock-launchd-main", "/sbin/launchd.ios", 0, "kernel initproc_spawned accepted");
    int observeServices = 0;
    PE_parse_boot_argn("houji_pid1_services", &observeServices, sizeof(observeServices));
    if (observeServices != 1 || !bootResultsEnabled()) return;
    // This worker is queued once by the main milestone. Use a separate
    // result so the first critical exit can still be saved independently.
    IOSleep(30000);
    if (!bootResultsEnabled()) return;
    const char *const names[] = {"notifyd", "backboardd", "runningboardd", "lsd", "SpringBoard"};
    uint32_t present = houji26_process_presence(names, sizeof(names) / sizeof(names[0]));
    uint64_t stats[7] = {};
    IOReturn gpu = native_adreno_provider_desktop_stats(stats);
    char detail[320];
    snprintf(detail, sizeof(detail),
        "notifyd=%u backboardd=%u runningboardd=%u lsd=%u SpringBoard=%u gpu-query=%x backboard-submits=%llu submit-errors=%llu completed-fence=%llu",
        present & 1U, (present >> 1) & 1U, (present >> 2) & 1U, (present >> 3) & 1U, (present >> 4) & 1U,
        static_cast<unsigned>(gpu), stats[0], stats[2], stats[4]);
    if (bootResultsEnabled()) saveBootResult("stock-launchd-services", "/sbin/launchd.ios", 0, detail);
}
extern "C" void houji26_boot_main(void) {
    if (__atomic_exchange_n(&bootMainSeen, 1U, __ATOMIC_ACQ_REL)) return;
    if (!bootResultsEnabled()) return;
    thread_t thread = THREAD_NULL;
    if (kernel_thread_start(bootMainWorker, nullptr, &thread) == KERN_SUCCESS) thread_deallocate(thread);
}
extern "C" void houji26_boot_reboot(const char *process, int options, const char *message) {
    bootResult("userspace-reboot-request", process, options, message, true);
}
extern "C" void houji26_boot_exit(unsigned name_space, uint64_t code, int status, const char *description, unsigned bytes) {
    if (!bootResultsEnabled()) return;
    if (bytes > 512) bytes = 512;
    if (!description) bytes = 0;
    char detail[640];
    snprintf(detail, sizeof(detail), "namespace=%u subcode=%llx description=%.*s", name_space,
        (unsigned long long)code, bytes ? (int)bytes : 4, bytes ? description : "none");
    bootResult("pid1-critical-exit", "initproc", status, detail, true);
}

extern "C" uint64_t houji_cpu_idle_mach(int cpu) { return hakios_cpu_idle_mach(cpu); }
extern "C" void houji_cpu_dvfs_start(void) { HoujiCpuDvfs::start(); }

static uint32_t proofStarted, proofMask, proofFailed;
static void proofWorker(void *argument, wait_result_t) {
    unsigned cpu = static_cast<unsigned>(reinterpret_cast<uintptr_t>(argument));
    processor_t processor = cpu_to_processor(cpu);
    if (processor == PROCESSOR_NULL) {
        __atomic_fetch_or(&proofFailed, 1U << cpu, __ATOMIC_RELEASE);
        return;
    }
    thread_bind(processor);
    thread_block(THREAD_CONTINUE_NULL);
    unsigned current = static_cast<unsigned>(cpu_number());
    uint64_t mpidr = __builtin_arm_rsr64("MPIDR_EL1") & 0xffffff;
    if (current != cpu || mpidr != static_cast<uint64_t>(cpu) << 8) {
        __atomic_fetch_or(&proofFailed, 1U << cpu, __ATOMIC_RELEASE);
    } else {
        __atomic_fetch_or(&proofMask, 1U << cpu, __ATOMIC_RELEASE);
    }
    thread_bind(PROCESSOR_NULL);
}

extern "C" bool houji_smp_preflight(void) {
    const ml_topology_info_t *topology = ml_get_topology_info();
    if (!topology || !topology->num_cpus || topology->num_cpus > 8 ||
        processor_avail_count != topology->num_cpus) return false;
    unsigned count = topology->num_cpus;
    for (unsigned cpu = 0; cpu < count; ++cpu)
        if (topology->cpus[cpu].phys_id != cpu << 8) return false;
    unsigned expected = 0;
    if (__atomic_compare_exchange_n(&proofStarted, &expected, 1U, false, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
        for (unsigned cpu = 0; cpu < count; ++cpu) {
            thread_t thread = THREAD_NULL;
            if (kernel_thread_start(proofWorker, reinterpret_cast<void *>(static_cast<uintptr_t>(cpu)), &thread) != KERN_SUCCESS) {
                __atomic_fetch_or(&proofFailed, 1U << cpu, __ATOMIC_RELEASE);
                return false;
            }
            thread_deallocate(thread);
        }
    }
    uint64_t deadline = 0;
    nanoseconds_to_deadline(5 * NSEC_PER_SEC, &deadline);
    const uint32_t wanted = (1U << count) - 1;
    while (__atomic_load_n(&proofMask, __ATOMIC_ACQUIRE) != wanted) {
        if (__atomic_load_n(&proofFailed, __ATOMIC_ACQUIRE) || mach_absolute_time() >= deadline) return false;
        IOSleep(1);
    }
    if (count > 1 && (houji26_running_mask() & wanted) != wanted) return false;
    for (unsigned cpu = 0; cpu < count; ++cpu) {
        uint64_t timer = 0, ipi = 0;
        houji26_irq_counts(cpu, &timer, &ipi);
        if (!timer || (count > 1 && !ipi)) return false;
    }
    return __atomic_load_n(&proofFailed, __ATOMIC_ACQUIRE) == 0;
}

extern "C" void houji_smp_report(void) {
    IOLog("HOUJI26 SMP: online=%u PSCI=%x workers=%x failed=%x; actual kernel state\n",
        processor_avail_count, houji26_running_mask(), proofMask, proofFailed);
    const ml_topology_info_t *topology = ml_get_topology_info();
    if (!topology) return;
    for (unsigned cpu = 0; cpu < topology->num_cpus; ++cpu) {
        uint64_t timer = 0, ipi = 0;
        houji26_irq_counts(cpu, &timer, &ipi);
        IOLog("HOUJI26 CPU%u: phys=%x timer=%llu ipi=%llu\n", cpu,
            topology->cpus[cpu].phys_id, timer, ipi);
    }
}
extern "C" void houji_cpu_scheduler_report(void) { houji_smp_report(); }

extern "C" void sweet_hid_touch(int finger, int down, int x, int y) {
    static uint32_t active;
    if (finger < 0 || finger >= 10 || x < 0 || x >= 1200 || y < 0 || y >= 2670) return;
    unsigned bit = 1U << finger;
    unsigned previous = __atomic_load_n(&active, __ATOMIC_ACQUIRE);
    unsigned kind = down ? ((previous & bit) ? REDWOOD_TOUCH_MOVE : REDWOOD_TOUCH_DOWN) : REDWOOD_TOUCH_UP;
    if (down) __atomic_fetch_or(&active, bit, __ATOMIC_ACQ_REL);
    else __atomic_fetch_and(&active, ~bit, __ATOMIC_ACQ_REL);
    redwood_touch_post(kind, finger, static_cast<uint32_t>(x) * 65535U / 1199U,
        static_cast<uint32_t>(y) * 65535U / 2669U);
    if (down) houji_dvfs_touch();
}

/* These are optional diagnostics of the old framework, not device functions.
 * Keep the quiet configuration explicit; do not fabricate old counters for
 * the new platform's MAC, sampler or network stack.
 */
extern "C" unsigned houji_sample_snapshot(char *, unsigned) { return 0; }
extern "C" unsigned houji_network_stack_snapshot(char *, unsigned) { return 0; }
extern "C" unsigned houji_tcc_extension_snapshot(char *, unsigned) { return 0; }
extern "C" void houji_sandbox_abi_report(void) {
    IOLog("HOUJI26 MAC: reference minimac; legacy iOS16 diagnostic counters unavailable\n");
}
