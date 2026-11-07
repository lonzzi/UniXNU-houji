#if defined(ARM64_BOARD_CONFIG_SWEET)
// SPDX-License-Identifier: BSD-2-Clause-Patent
// Read transport from pinned UniXNU Ace 5/NativeUfs; houji-specific GPT-only probe.
// No IOBlockStorageDevice bindings. Optional preallocated-file diagnostic writer.
#include <IOKit/IOService.h>
#include <IOKit/IOPlatformExpert.h>
#include <libkern/crypto/sha2.h>
#include <vm/vm_kern.h>
#include <vm/vm_map.h>
#include <sys/param.h>
#include <sys/msgbuf.h>
#include <sys/memdev_ops.h>
#include <sys/errno.h>
#if defined(NATIVE_VERIFIED_BLOCK) && NATIVE_VERIFIED_BLOCK
#include "verified_root.h"
#include "VerifiedBlockPolicy.h"
extern "C" {
extern const struct vr_crypto vr_xnu_crypto;
extern const uint8_t verified_root_key[];
}
#endif
#include <libkern/c++/OSNumber.h>
#include <libkern/c++/OSIterator.h>
#include <IOKit/IOBSD.h>
#include <kern/thread.h>
#include <kern/clock.h>
#include <IOKit/IOLib.h>
#include <IOKit/IOBufferMemoryDescriptor.h>
#include <IOKit/IOMemoryDescriptor.h>
#include <IOKit/IOLib.h>
#include <libkern/c++/OSData.h>
#include <libkern/c++/OSString.h>
#include <libkern/libkern.h>
#include <pexpert/pexpert.h>
#include <kern/debug.h>
#include "GccUfs.h"
#include "RootDiagnostics.h"
#include "TransferPoll.h"
#include "GiuliaSmmuTakeover.h"
#include "HoujiCpuFrequency.h"
#include "HoujiBatterySurvey.inc"

static UfsRootDiagnostics gRootDiagnostic;
static bool gRootDiagnosticEnabled;
extern unsigned long gPhysBase, gPhysSize;
extern "C" void houji_smp_report(void);
extern "C" void houji_cpu_dvfs_start(void);
extern "C" bool houji_smp_preflight(void);
static IOLock *gCheckpointLock;
static void rootDiagnosticFailure (const char *reason);
static char gNativeText[4096];
static unsigned gNativeTextBytes;
static void nativeStage(const char *stage) {
  gRootDiagnostic.stage=stage;
  IOLog("HOUJI NATIVE UFS: %s\n",stage);
  if(gNativeTextBytes<sizeof(gNativeText)-1) {
    int n=snprintf(gNativeText+gNativeTextBytes,sizeof(gNativeText)-gNativeTextBytes,
                   "HOUJI NATIVE UFS: %s\n",stage);
    if(n>0)gNativeTextBytes+=unsigned(n)<sizeof(gNativeText)-gNativeTextBytes ?
      unsigned(n) : sizeof(gNativeText)-gNativeTextBytes-1;
  }
}

static uint32_t read32 (uintptr_t base, unsigned offset)
{
  return *reinterpret_cast<volatile uint32_t *> (base + offset);
}

static void write32 (uintptr_t base, unsigned offset, uint32_t value)
{
  *reinterpret_cast<volatile uint32_t *> (base + offset) = value;
}

static uintptr_t mapPhys (uint64_t phys, uint64_t length, OSSharedPtr<IOMemoryMap> &keep)
{
  IODeviceMemory *memory = IODeviceMemory::withRange (phys, length);
  if (!memory) {
    return 0;
  }
  keep = OSSharedPtr<IOMemoryMap> (memory->map (kIOMapAnywhere | kIOMapInhibitCache), OSNoRetain);
  memory->release ();
  return keep ? static_cast<uintptr_t> (keep->getVirtualAddress ()) : 0;
}

static void smmuBypassStream (uint16_t stream, OSSharedPtr<IOMemoryMap> &keep, uint32_t *report,
                              uint64_t dma, uint64_t bytes, uintptr_t host)
{
  if ((read32(host, 0x60) & 1) || read32(host, 0x58) || read32(host, 0x78))
    panic("HOUJI NATIVE UFS: queue not quiescent before SMMU takeover");
  uintptr_t smmu = mapPhys (0x15000000ULL, 0xd0000, keep);
  if (!smmu) {
    panic ("NATIVE_UFS smmu: map failed");
  }
  GiuliaSmmuPlan selection = planGiuliaSmmu (stream, dma, bytes, [smmu](unsigned offset) { return read32 (smmu, offset); });
  const UfsSmmuPlan &plan = selection.target;
  gRootDiagnostic.smmuId1 = read32 (smmu, 0x24);
  gRootDiagnostic.smmuId2 = read32 (smmu, 0x28);
  gRootDiagnostic.smmuSlot = plan.slot;
  if (plan.error) {
    nativeStage("SMMU planning");
    gRootDiagnostic.result = static_cast<uint32_t> (kIOReturnNotReady);
    gRootDiagnostic.smr = plan.smr; gRootDiagnostic.s2cr = plan.s2cr;
    rootDiagnosticFailure (plan.error);
    panic ("NATIVE_UFS smmu: %s slot=%u smr=%x s2cr=%x", plan.error, plan.slot, plan.smr, plan.s2cr);
  }
  unsigned bank = plan.bank, index = plan.slot;
  uintptr_t cb = smmu + (plan.pages + bank) * 4096;
  const char *failure = configureGiuliaSmmu (selection, stream, true,
    [smmu](unsigned offset) { return read32 (smmu, offset); },
    [smmu](unsigned offset, uint32_t value) {
      write32 (smmu, offset, value);
      __asm__ volatile ("dsb sy" ::: "memory");
    });
  uint32_t smr = read32 (smmu, 0x800 + 4 * index), s2cr = read32 (smmu, 0xc00 + 4 * index);
  uint32_t cbar = read32 (smmu, 0x1000 + 4 * bank), sctlr = read32 (cb, 0x0);
  gRootDiagnostic.smr = smr; gRootDiagnostic.s2cr = s2cr;
  gRootDiagnostic.cbar = cbar; gRootDiagnostic.sctlr = sctlr;
  gRootDiagnostic.smmuId2 = read32 (smmu, 0x28);
  gRootDiagnostic.cba2r = read32 (smmu, 0x1800 + 4 * bank);
  if (failure) {
    nativeStage("SMMU readback");
    gRootDiagnostic.result = static_cast<uint32_t> (kIOReturnNotReady);
    rootDiagnosticFailure (failure);
    panic ("NATIVE_UFS smmu: %s idx=%u bank=%u cba2r=%x", failure, index, bank,
           gRootDiagnostic.cba2r);
  }
  report[0] = index | (bank << 16);
  report[1] = cbar;
  report[2] = sctlr;
}

static bool houjiNativeGpuFsAllowed(void);
namespace {
#include "RootTransportLayout.h"

struct UfsHost {
  OSSharedPtr<IOMemoryMap> hcMap, smmuMap;
  uintptr_t hc;
  IOBufferMemoryDescriptor *dma;
  uint8_t *virt;
  uint64_t phys;
  IOLock *lock;
  bool ready;
  bool poisoned = false;
  uintptr_t smmu = 0, cb = 0;
};
UfsHost gHost = { OSSharedPtr<IOMemoryMap> (), OSSharedPtr<IOMemoryMap> (), 0, nullptr, nullptr, 0, nullptr, false };
static bool gLogEnabled, gLogArmed, gRootEnabled, gRootArmed, gReturnRecovery, gRecoveryArmed;
static bool gGptVerified, gFlagsInitialized;
static uint32_t gLogExpectedCrc;

static IOReturn recordTransport (IOReturn result, uint32_t ocs)
{
  gRootDiagnostic.result = static_cast<uint32_t> (result);
  gRootDiagnostic.ocs = ocs;
  if (result != kIOReturnSuccess && gRootDiagnosticEnabled) {
    // Read only the already mapped HC's status/registers used by this driver.
    gRootDiagnostic.hcs = read32 (gHost.hc, 0x30);
    gRootDiagnostic.hce = read32 (gHost.hc, 0x34);
    gRootDiagnostic.doorbell = read32 (gHost.hc, 0x58);
    gRootDiagnostic.interrupt = read32 (gHost.hc, 0x20);
    gRootDiagnostic.list = uint64_t (read32 (gHost.hc, 0x50)) |
                          (uint64_t (read32 (gHost.hc, 0x54)) << 32);
    gRootDiagnostic.runStop = read32 (gHost.hc, 0x60);
    gRootDiagnostic.ahit = read32 (gHost.hc, 0x18);
    for (unsigned i = 0; i < 5; ++i) gRootDiagnostic.uic[i] = read32 (gHost.hc, 0x38 + 4 * i);
    if (gHost.smmu && gHost.cb) {
      gRootDiagnostic.gfsr = read32 (gHost.smmu, 0x48);
      gRootDiagnostic.gsyn = read32 (gHost.smmu, 0x54);
      gRootDiagnostic.fsr = read32 (gHost.cb, 0x58);
      gRootDiagnostic.fsyn = read32 (gHost.cb, 0x68);
      gRootDiagnostic.far = uint64_t (read32 (gHost.cb, 0x60)) |
                            (uint64_t (read32 (gHost.cb, 0x64)) << 32);
    }
  }
  return result;
}

static void cacheCivac (const void *start, size_t length)
{
  uintptr_t address = reinterpret_cast<uintptr_t> (start) & ~uintptr_t (63);
  uintptr_t end = reinterpret_cast<uintptr_t> (start) + length;
  for (; address < end; address += 64) {
    __asm__ volatile ("dc civac, %0" :: "r" (address) : "memory");
  }
  __asm__ volatile ("dsb sy" ::: "memory");
}

// Device-owned cache lines must not be written back over DMA results. All
// callers use complete 64-byte lines from the dedicated page-aligned buffer.
static void cacheInvalidate (const void *start, size_t length)
{
  __asm__ volatile ("dsb sy" ::: "memory");
  uintptr_t address = reinterpret_cast<uintptr_t> (start);
  for (uintptr_t end = address + length; address < end; address += 64) {
    __asm__ volatile ("dc ivac, %0" :: "r" (address) : "memory");
  }
  __asm__ volatile ("dsb sy" ::: "memory");
}

static void hostWrite (unsigned offset, uint32_t value)
{
  __asm__ volatile ("dsb sy" ::: "memory");
  write32 (gHost.hc, offset, value);
  __asm__ volatile ("dsb sy" ::: "memory");
}

#include "RootQueue.h"

#define HOUJI_GPU_FILESYSTEM_GATE 1
#include "RootCommands.h"

// Bring the live controller up for reading: map, SMMU pass-through for the UFS stream, take over
// the request list, wake the device from UFS-PowerDown and wait for LUN 0 to be ready.
// `why` receives a short reason on failure.
static bool hostBringUp (const char **why)
{
  if (gHost.poisoned) {
    *why = "transport quarantined";
    return false;
  }
  if (gHost.ready) {
    return true;
  }
  nativeStage("controller map");
  gHost.hc = mapPhys (UFS_HC_BASE, UFS_HC_SIZE, gHost.hcMap);
  if (!gHost.hc) {
    *why = "controller map";
    return false;
  }
  if (gRootDiagnosticEnabled) {
    gRootDiagnostic.cap = read32 (gHost.hc, 0x00);
    gRootDiagnostic.version = read32 (gHost.hc, 0x08);
    gRootDiagnostic.hcs = read32 (gHost.hc, 0x30);
    gRootDiagnostic.hce = read32 (gHost.hc, 0x34);
  }
  nativeStage("DMA allocation");
  gHost.dma = IOBufferMemoryDescriptor::inTaskWithOptions (
    kernel_task, static_cast<IOOptionBits> (kIODirectionInOut) | static_cast<IOOptionBits> (kIOMemoryPhysicallyContiguous) |
    static_cast<IOOptionBits> (kIOMemoryMapperNone),
    kDmaBytes, 4096);
  gHost.lock = IOLockAlloc ();
  if (!gHost.dma || !gHost.lock) {
    *why = "dma buffer";
    return false;
  }
  gHost.virt = static_cast<uint8_t *> (gHost.dma->getBytesNoCopy ());
  IOByteCount contiguous = 0;
  gHost.phys = gHost.dma->getPhysicalSegment (0, &contiguous, kIOMemoryMapperNone);
  gRootDiagnostic.dma = gHost.phys;
  gRootDiagnostic.contiguous = contiguous;
  if (!gHost.virt || !gHost.phys || (gHost.phys & 4095) || contiguous < kDmaBytes ||
      (!(read32 (gHost.hc, 0) & 0x01000000) && gHost.phys + kDmaBytes > 0x100000000ULL)) {
    *why = "DMA address/contiguity";
    return false;
  }
  bzero (gHost.virt, kDmaBytes);
  cacheCivac (gHost.virt, kDmaBytes);
  uint32_t report[3] = { 0, 0, 0 };
  nativeStage("SMMU and request list");
  if (!hostStopQueue (why)) return false;
  smmuBypassStream (0x60, gHost.smmuMap, report, gHost.phys, kDmaBytes, gHost.hc);
  gHost.smmu = gHost.smmuMap->getVirtualAddress ();
  uint32_t idr1 = read32 (gHost.smmu, 0x24);
  gHost.cb = gHost.smmu + ((1U << (((idr1 >> 28) & 7) + 1)) + (report[0] >> 16)) * 4096;
  if (gRootDiagnosticEnabled) {
    gRootDiagnostic.gfsrBefore = read32 (gHost.smmu, 0x48);
    gRootDiagnostic.fsrBefore = read32 (gHost.cb, 0x58);
    gRootDiagnostic.smmuId2 = read32 (gHost.smmu, 0x28);
    gRootDiagnostic.cba2r = read32 (gHost.smmu, 0x1800 + 4 * (report[0] >> 16));
  }
  if (!hostStartQueue (why)) return false;
  nativeStage("NOP OUT");
  // NOP OUT proves the transport before touching the SCSI layer.
  if (hostNop () != kIOReturnSuccess) {
    gHost.ready = false;
    *why = "NOP OUT";
    return false;
  }
  uint32_t power = 0;
  nativeStage("power mode query");
  if (hostQueryRead (0x03, 0x02, &power) != kIOReturnSuccess) {
    gHost.ready = false;
    *why = "power mode query";
    return false;
  }
  gRootDiagnostic.power = power;
  if ((power & 0xff) != 0x11) {
    nativeStage("device wake");
    // START STOP UNIT, power condition ACTIVE, to the UFS Device well-known LU (as Linux resumes).
    uint8_t ssu[6] = { 0x1b, 0, 0, 0, 0x10, 0 };
    uint32_t scsi = 0;
    if (hostScsi (0xd0, ssu, 6, false, 0, 500000, &scsi) != kIOReturnSuccess) {
      gHost.ready = false;
      *why = "wake (START STOP UNIT)";
      return false;
    }
    IODelay (50000);
  }
  nativeStage("LUN ready");
  for (unsigned attempt = 0; attempt < 20; attempt++) {
    uint8_t tur[6] = { 0, 0, 0, 0, 0, 0 };
    uint32_t scsi = 0;
    if (hostScsi (0, tur, 6, false, 0, 200000, &scsi) == kIOReturnSuccess) {
      gHost.ready = true;
      return true;
    }
    if (gHost.poisoned) break;
    IODelay (10000);
  }
  gHost.ready = false;
  *why = "LUN 0 not ready";
  return false;
}
}

#include "HoujiGptCheck.h"
#include "HoujiLogFile.h"
#include "HoujiLogCommit.h"
#include "HoujiRootWindow.h"
#include "HoujiFirmwareUfs.inc"
#include "HoujiFirmwareProbe.inc"

extern "C" bool bsd_log_lock(bool);
extern "C" void bsd_log_unlock(void);

// Normal BSD initialization context only. Never invoked from a panic handler,
// interrupt, or while owning the log lock. No allocation/I/O while locked.
static unsigned copyKernelMessages(char *dst,unsigned capacity) {
  unsigned done=0;
  bsd_log_lock(true);
  const struct msgbuf *m=msgbufp;
  if(m && m->msg_magic==MSG_MAGIC && m->msg_bufc && m->msg_size>0 &&
     m->msg_bufx>=0 && m->msg_bufx<m->msg_size) {
    unsigned size=unsigned(m->msg_size), count=size<capacity?size:capacity;
    unsigned at=(unsigned(m->msg_bufx)+size-count)%size;
    for(unsigned i=0;i<count;i++) {
      char c=m->msg_bufc[at];at=(at+1)%size;
      if(c)dst[done++]=c;
    }
  }
  bsd_log_unlock();
  return done;
}

static IOReturn writeLogBlock(unsigned index,const uint8_t *data) {
  if(!gLogArmed || !gHost.ready || gHost.poisoned || index>=16 || !data) return kIOReturnNotPermitted;
  IOLockLock(gHost.lock);
  if(!hostClearUcd()) {IOLockUnlock(gHost.lock);return kIOReturnNotReady;}
  memcpy(gHost.virt+kDataOffset,data,4096);
  uint32_t lba=kHoujiLogLbas[index],scsi=0;
  uint8_t cdb[10]={0x2a,0,uint8_t(lba>>24),uint8_t(lba>>16),uint8_t(lba>>8),uint8_t(lba),0,0,1,0};
  IOReturn r=hostScsiDir(0,cdb,10,1,4096,1000000,&scsi);
  IOLockUnlock(gHost.lock);
  return r;
}
static IOReturn flushLog(void) {
  if(!gLogArmed || !gHost.ready || gHost.poisoned) return kIOReturnNotPermitted;
  IOLockLock(gHost.lock);
  uint8_t cdb[10]={0x35,0,0,0,0,0,0,0,0,0};uint32_t scsi=0;
  IOReturn r=hostScsiDir(0,cdb,10,0,0,1000000,&scsi);
  IOLockUnlock(gHost.lock);
  return r;
}
extern "C" unsigned houji_gpu_cached_timing(char *,unsigned);
extern "C" unsigned houji_framebuffer_snapshot(char *,unsigned);
extern "C" unsigned houji_display_timing_snapshot(char *,unsigned);
extern "C" unsigned houji_sample_snapshot(char *,unsigned);
extern "C" unsigned houji_network_stack_snapshot(char *,unsigned);
extern "C" unsigned houji_cpu_dvfs_snapshot(char *,unsigned);
extern "C" unsigned houji_adsp_snapshot(char *,unsigned);
#if HOUJI_IOS26 && (DEVELOPMENT || DEBUG)
extern "C" unsigned houji26_wallpaper_snapshot(char *,unsigned);
#endif
static const char *saveNativeLog(__unused const char *stage) {
  static uint8_t metadata[7*4096],image[65536];
  constexpr unsigned gpuCapacity=20480,displayCapacity=4096,sampleCapacity=12800;
  constexpr unsigned networkCapacity=512,cpuCapacity=1024,batteryCapacity=2048,panelCapacity=4096;
  constexpr unsigned wallpaperCapacity=3072;
  static_assert(sizeof(gNativeText)+gpuCapacity+displayCapacity+sampleCapacity+
    networkCapacity+cpuCapacity+batteryCapacity+panelCapacity+wallpaperCapacity+128<sizeof(image)-4096,
    "snapshot prefixes exceed existing log file");
  nativeStage("logfs metadata read");
  for(unsigned i=0;i<7;i++)
    if(hostRead4k(0,4096+i,1,metadata+i*4096)!=kIOReturnSuccess) return "log metadata read failed";
  const char *why=houjiLogFatError(metadata,sizeof(metadata));
  if(why)return why;
  nativeStage("reserved file allocation verified");
  for(unsigned i=0;i<16;i++)
    if(hostRead4k(0,kHoujiLogLbas[i],1,image+i*4096)!=kIOReturnSuccess) return "initial log file read failed";
  if(houjiCrc32(image,sizeof(image))!=(gLogExpectedCrc?gLogExpectedCrc:kHoujiLogInitialCrc))return "log file ownership/run ID changed";
  nativeStage("READ PASS; saving XNU snapshot before diagnostic stop");
  bzero(image,sizeof(image));
  unsigned used=gNativeTextBytes;
  memcpy(image+4096,gNativeText,used);
  used+=houji_gpu_cached_timing(reinterpret_cast<char *>(image+4096+used),gpuCapacity);
  used+=houji_framebuffer_snapshot(reinterpret_cast<char *>(image+4096+used),displayCapacity);
  used+=houji_display_timing_snapshot(reinterpret_cast<char *>(image+4096+used),panelCapacity);
  used+=houji_sample_snapshot(reinterpret_cast<char *>(image+4096+used),sampleCapacity);
  used+=houji_network_stack_snapshot(reinterpret_cast<char *>(image+4096+used),networkCapacity);
  used+=houji_cpu_dvfs_snapshot(reinterpret_cast<char *>(image+4096+used),cpuCapacity);
  unsigned batteryUsed=houjiBatterySurveySnapshot(reinterpret_cast<char *>(image+4096+used),batteryCapacity);
  used+=batteryUsed;
  unsigned fwUsed=houjiFwProbeSnapshot(reinterpret_cast<char *>(image+4096+used),batteryCapacity-batteryUsed);
  used+=fwUsed;batteryUsed+=fwUsed;
  used+=houji_adsp_snapshot(reinterpret_cast<char *>(image+4096+used),batteryCapacity-batteryUsed);
#if HOUJI_IOS26 && (DEVELOPMENT || DEBUG)
  static const char gpuEnd[]="HOUJI26 STARTUP RESULT: gpu-runtime-end ";
  static const char services[]="HOUJI26 STARTUP RESULT: stock-launchd-services ";
  if(stage && (!strncmp(stage,gpuEnd,sizeof(gpuEnd)-1) || !strncmp(stage,services,sizeof(services)-1)))
    used+=houji26_wallpaper_snapshot(reinterpret_cast<char *>(image+4096+used),wallpaperCapacity);
#endif
  static const char marker[]="\n--- XNU kernel message buffer (tail; NULs removed) ---\n";
  memcpy(image+4096+used,marker,sizeof(marker)-1);used+=sizeof(marker)-1;
  used+=copyKernelMessages(reinterpret_cast<char *>(image+4096+used),sizeof(image)-4096-used);
  houjiLogHeader(image,image+4096,used);
  // The armed window contains only the 16 preallocated file data sectors.
  // Commit header last, with a cache flush at each boundary. No FAT writes.
  gLogArmed=true;
  why=houjiCommitLog(image,sizeof(image),
    [](unsigned i,const uint8_t *p){return writeLogBlock(i,p)==kIOReturnSuccess;},
    [](){return flushLog()==kIOReturnSuccess;},
    [](unsigned i,uint8_t *p){return hostRead4k(0,kHoujiLogLbas[i],1,p)==kIOReturnSuccess;});
  gLogArmed=false;
  if(why)return why;
  gLogExpectedCrc=houjiCrc32(image,sizeof(image));
  IOLog("HOUJI XNU LOG: COMMIT/FLUSH/READBACK PASS bytes=%u crc=%08x\n",used,houjiLe32(image+36));
  return nullptr;
}
static const char *setRecoveryBoot(void);
static void logTrialReset(void) {
  const char *why=setRecoveryBoot();
  if(why)rootDiagnosticFailure(why);
  // Real houji FDT /soc/psci: compatible arm,psci-1.0, method smc.
  // Only after successful log and recovery-BCB readback; no active-slot changes.
  IOLog("HOUJI XNU LOG: verified snapshot; resetting through PSCI (recovery BCB requested)\n");
  __asm__ volatile("dsb sy" ::: "memory");
  register uint64_t x0 __asm__("x0")=0x84000009U;
  __asm__ volatile("smc #0" : "+r"(x0) :: "x1","x2","x3","x4","x5","x6","x7",
                   "x8","x9","x10","x11","x12","x13","x14","x15","x16","x17","cc","memory");
  panic("HOUJI XNU LOG: saved, PSCI reset unexpectedly returned %llx",x0);
}
static void rootDiagnosticFailure(const char *why) {
  char text[2048];
  formatUfsRootDiagnostics(text,sizeof(text),gRootDiagnostic,why);
  panic("HOUJI NATIVE UFS READ FAILED\n%s",text);
}
static void initializeNativeFlags(void) {
  if(gFlagsInitialized)return;
  // The first call precedes launchd and its observer. Keep the reusable log
  // buffers and expected-file CRC under one lock once those threads can run.
  gCheckpointLock=IOLockAlloc();
  if(!gCheckpointLock)panic("HOUJI checkpoint lock allocation");
  gFlagsInitialized=true;gRootDiagnosticEnabled=true;
  int v=0;PE_parse_boot_argn("houji_ufs_log",&v,sizeof(v));gLogEnabled=v==1;
  v=0;PE_parse_boot_argn("houji_native_root",&v,sizeof(v));gRootEnabled=v==1;
  v=0;PE_parse_boot_argn("houji_return_recovery",&v,sizeof(v));gReturnRecovery=v==1;
}
static void ensureNative(void) {
  initializeNativeFlags();
  if(gGptVerified)return;
  const char *why="unknown";
  if(!hostBringUp(&why))rootDiagnosticFailure(why);
  IOLog("HOUJI MEMORY: XNU physical base=%llx bytes=%llu; UFS DMA=%llx bytes=%u\n",
        (uint64_t)gPhysBase,(uint64_t)gPhysSize,gHost.phys,(unsigned)kDmaBytes);
  static uint8_t header[4096],entries[8192];
  nativeStage("read primary GPT header");
  if(hostRead4k(0,1,1,header)!=kIOReturnSuccess)rootDiagnosticFailure("primary GPT read");
  nativeStage("read all 64 GPT entries");
  if(hostRead4k(0,2,2,entries)!=kIOReturnSuccess)rootDiagnosticFailure("GPT entry array read");
  why=houjiGptError(header,sizeof(header),entries,sizeof(entries));
  if(why)rootDiagnosticFailure(why);
  gGptVerified=true;
  nativeStage("native GPT verified 631fc7ea/9f1bb496");
}
static IOReturn writeNativeBlocks(uint32_t lba,unsigned blocks,const uint8_t *data) {
  if(!gHost.ready || gHost.poisoned || !data || !blocks || blocks>32)return kIOReturnNotReady;
  IOLockLock(gHost.lock);
  if(!hostClearUcd()){IOLockUnlock(gHost.lock);return kIOReturnNotReady;}
  memcpy(gHost.virt+kDataOffset,data,blocks*4096U);
  uint32_t scsi=0;
  uint8_t cdb[10]={0x2a,0,uint8_t(lba>>24),uint8_t(lba>>16),uint8_t(lba>>8),uint8_t(lba),0,uint8_t(blocks>>8),uint8_t(blocks),0};
  IOReturn r=hostScsiDir(0,cdb,10,1,blocks*4096U,1000000,&scsi);
  IOLockUnlock(gHost.lock);return r;
}
static IOReturn flushNative(void) {
  if(!gHost.ready || gHost.poisoned)return kIOReturnNotReady;
  IOLockLock(gHost.lock);
  uint8_t cdb[10]={0x35,0,0,0,0,0,0,0,0,0};uint32_t scsi=0;
  IOReturn r=hostScsiDir(0,cdb,10,0,0,1000000,&scsi);
  IOLockUnlock(gHost.lock);return r;
}
#include "HoujiRecoveryRecord.h"
static const char *setRecoveryBoot(void) {
  if(!gReturnRecovery)return nullptr;
  static uint8_t before[4096],after[4096],back[4096];
  if(hostRead4k(0,1024,1,before)!=kIOReturnSuccess)return "BCB read";
  memcpy(after,before,sizeof(after));
  if(!houjiRecoveryRecord(after,sizeof(after)))return "BCB build";
  gRecoveryArmed=true;
  IOReturn r=writeNativeBlocks(1024,1,after);
  if(r==kIOReturnSuccess)r=flushNative();
  gRecoveryArmed=false;
  if(r!=kIOReturnSuccess)return "BCB write/flush";
  if(hostRead4k(0,1024,1,back)!=kIOReturnSuccess || memcmp(after,back,sizeof(back)))return "BCB readback";
  IOLog("HOUJI RECOVERY: BCB verified; no wipe arguments; reserved/A-B data preserved\n");
  return nullptr;
}
extern "C" void houji_ufs_checkpoint(const char *stage,unsigned stopId) {
  initializeNativeFlags();
  if(!gLogEnabled && !gRootEnabled)return;
  IOLockLock(gCheckpointLock);
  ensureNative();nativeStage(stage);
  int target=0;PE_parse_boot_argn("houji_checkpoint_stop",&target,sizeof(target));
  bool panicProbeArmed=false;
  if(stopId==3 && target==3 && gLogEnabled && gReturnRecovery) {
    int probe=0;
    if(PE_parse_boot_argn("houji_panic_probe",&probe,sizeof(probe)) && probe==1) {
      // Normal initialization context: finish the existing no-wipe BCB RMW
      // and full readback before the log commit or deliberate panic.
      const char *why=setRecoveryBoot();
      if(why)rootDiagnosticFailure(why);
      nativeStage("panic recovery self-test armed; verified BCB, panic after successful log commit");
      panicProbeArmed=true;
    }
  }
  if(gLogEnabled){const char *why=saveNativeLog(stage);if(why)rootDiagnosticFailure(why);}
  if(panicProbeArmed) {
    // The saved intent is not evidence that panic happened. Only reach this
    // distinct panic after successful commit/flush/readback; panic does no I/O.
    IOLockUnlock(gCheckpointLock);
    panic("HOUJI PANIC RECOVERY SELF-TEST: checkpoint3 log commit/readback passed");
  }
  if(stopId && target==int(stopId))logTrialReset();
  IOLockUnlock(gCheckpointLock);
}
static IOLock *gRootWindowLock;
static uint8_t *gRootBounce;
static void reportCpuFrequencyReadOnly(void) {
  // Captured houji FDT: CPU0 -> EPSS domain 0, 0x17d91000 + 0x1000.
  // Register facts cross-checked with MiCode shennong-u-oss cpufreq driver:
  // EPSS cycle base 0x3c4 + (core_id + 1)*4, hence CPU0 at 0x3c8.
  OSSharedPtr<IOMemoryMap> keep;
  uintptr_t base = mapPhys(0x17d91000ULL, 0x1000, keep);
  if (!base) { IOLog("HOUJI CPU FREQ: EPSS map unavailable\n"); return; }
  uint32_t enable = read32(base,0), control = read32(base,0xb0);
  uint32_t state = read32(base,0x20), index = read32(base,0x320);
  uint64_t midr, mpidr, reference, start, finish;
  __asm__ volatile("mrs %0, midr_el1" : "=r"(midr));
  __asm__ volatile("mrs %0, mpidr_el1" : "=r"(mpidr));
  __asm__ volatile("mrs %0, cntfrq_el0" : "=r"(reference));
  IOLog("HOUJI CPU FREQ: RO MIDR=%llx MPIDR=%llx enable=%x control=%x state=%x requested_index=%u reference_hz=%llu\n",
        midr,mpidr,enable,control,state,index,reference);
  if ((mpidr & 0xff00ffffffULL) || !(enable & 1) || index >= 40 ||
      reference < 1000000 || reference > 1000000000) {
    IOLog("HOUJI CPU FREQ: RO sample refused; domain/core/state not validated\n"); return;
  }
  uint32_t lut = read32(base,0x100 + index*4);
  clock_get_uptime(&start);
  uint32_t before = read32(base,0x3c8);
  do {
    __asm__ volatile("yield" ::: "memory");
    clock_get_uptime(&finish);
    if (finish < start) {
      IOLog("HOUJI CPU FREQ: RO kernel reference moved backwards; refusing sample\n");
      return;
    }
  } while (finish - start < reference/10);
  uint32_t after = read32(base,0x3c8), khz = 0;
  clock_get_uptime(&finish);
  if (houjiCpuFrequencyKHz(before,after,finish-start,reference,&khz))
    IOLog("HOUJI CPU FREQ: RO CPU0 busy-average=%u kHz cycles=%u ticks=%llu LUT=%08x; no frequency/voltage writes\n",
          khz,uint32_t(after-before),finish-start,lut);
  else
    IOLog("HOUJI CPU FREQ: RO counter sample unavailable before=%x after=%x ticks=%llu LUT=%08x\n",
          before,after,finish-start,lut);
}
static int nativeRootTransfer(void *,uint64_t offset,void *data,size_t bytes,int write) {
  if((write!=0 && write!=1) || (!data && bytes) || offset>kHoujiRootBytes || bytes>kHoujiRootBytes-offset ||
     ((offset|bytes)&511))return EINVAL;
  if(!gRootArmed || !gRootWindowLock || !gRootBounce)return ENXIO;
  struct Operations {
    bool read(uint32_t lba,unsigned blocks,uint8_t *p){return hostRead4k(0,lba,blocks,p)==kIOReturnSuccess;}
    bool write(uint32_t lba,unsigned blocks,const uint8_t *p){return writeNativeBlocks(lba,blocks,p)==kIOReturnSuccess;}
  } ops;
  IOLockLock(gRootWindowLock);
  int result=houjiRootTransfer(ops,offset,data,bytes,write!=0,gRootBounce);
  IOLockUnlock(gRootWindowLock);
  return result==0?0:result==1?EINVAL:EIO;
}
static int nativeRootSynchronize(void *) {
  if(!gRootArmed || !gRootWindowLock)return ENXIO;
  IOLockLock(gRootWindowLock);IOReturn r=flushNative();IOLockUnlock(gRootWindowLock);
  return r==kIOReturnSuccess?0:EIO;
}
#include "HoujiTouchProbe.inc"
#include "HoujiTouchDriver.inc"
#include "HoujiGpuProbe.inc"
#include "HoujiCpuPageProbe.inc"
#include "HoujiGpuMemoryProbe.inc"
#include "HoujiGpuTableProbe.inc"
#include "HoujiGpuHandoffProbe.inc"
#include "HoujiGpuHeldTcmProbe.inc"
#include "HoujiGpuInternalExecProbe.inc"
#include "HoujiGpuExternalCanaryProbe.inc"
#include "HoujiGpuFirmwareMemoryProbe.inc"
#include "HoujiGpuSecureCxProbe.inc"
#include "HoujiGpuStockFirmwareProbe.inc"
#include "HoujiGpuFullHostProbe.inc"
extern "C" void houji_ufs_root_attach(void) {
  initializeNativeFlags();if(!gRootEnabled)return;
  static bool attached;if(attached)return;
  ensureNative();nativeStage("native root HFSX identity read");
  static uint8_t page[4096];
  if(hostRead4k(0,kHoujiRootFirst,1,page)!=kIOReturnSuccess || !houjiHfsRootHeader(page,sizeof(page)))
    rootDiagnosticFailure("root HFSX geometry/volume ID does not match installed image");
  gRootWindowLock=IOLockAlloc();gRootBounce=static_cast<uint8_t *>(IOMalloc(131072));
  if(!gRootWindowLock || !gRootBounce)rootDiagnosticFailure("root buffer allocation");
  static const struct mdev_ops ops={nativeRootTransfer,nativeRootSynchronize};
  dev_t device=mdevadd_ops(0,kHoujiRootBytes,&ops,nullptr);
  if(device==(dev_t)-1)rootDiagnosticFailure("native block registration");
  gRootArmed=true;attached=true;
  IOLog("HOUJI ROOT: md0 is native UFS LUN0 [%u,%u), bytes=%llu; no RAM root backing\n",
        kHoujiRootFirst,kHoujiRootFirst+kHoujiRootBlocks,kHoujiRootBytes);
  if (!houji_smp_preflight()) {
#if HOUJI_IOS26
    houji_smp_report();
#endif
    houji_ufs_checkpoint("SMP preflight failed; HFS not mounted",0);
    rootDiagnosticFailure("SMP worker/timer/SGI verification");
  }
  houji_smp_report();
  houji_ufs_checkpoint("before CPU factory EPSS governor initialization; SMP proof complete",0);
  houji_cpu_dvfs_start();
  houji_ufs_checkpoint("after CPU factory EPSS governor initialization",0);
  houjiBatterySurvey();
  int frequencyProbe=0;
  PE_parse_boot_argn("houji_cpu_freq_probe",&frequencyProbe,sizeof(frequencyProbe));
  if (frequencyProbe==1) {
    houji_ufs_checkpoint("before read-only EPSS CPU0 frequency probe",0);
    reportCpuFrequencyReadOnly();
  }
  houjiGpuCpuPageProbeOnce();
  houjiGpuMemoryProbeOnce();
  houjiGpuTableProbeOnce();
  int handoffProbe=0;PE_parse_boot_argn("houji_gpu_handoff_probe",&handoffProbe,sizeof(handoffProbe));
  if(handoffProbe==1) {
    houji_ufs_checkpoint("before XNU GPU power-handoff read-only observation",0);
    houjiGpuHandoffProbeOnce();
  }
  int heldTcmProbe=0;PE_parse_boot_argn("houji_gpu_held_tcm_probe",&heldTcmProbe,sizeof(heldTcmProbe));
  if(heldTcmProbe==1) {
    houji_ufs_checkpoint("before XNU held-reset GMU internal TCM trial",0);
    if(!houjiGpuHeldTcmProbeOnce())
      panic("HOUJI GPU HELD TCM: qualification uncertain; retaining mappings/backup, PSCI-only recovery without later filesystem save");
  }
  houjiGpuProbeOnce();
  touchProbeOnce();
  int internalExecProbe=0;PE_parse_boot_argn("houji_gpu_internal_exec_probe",&internalExecProbe,sizeof(internalExecProbe));
  if(internalExecProbe==1) {
    houji_ufs_checkpoint("before XNU internal CM3 fixed-code execution trial",0);
    if(!houjiGpuInternalExecProbeOnce())
      panic("HOUJI GMU EXEC: qualification uncertain; resources and originals retained; PSCI-only recovery without later filesystem save");
  }
  int externalCanaryProbe=0;PE_parse_boot_argn("houji_gpu_external_canary_probe",&externalCanaryProbe,sizeof(externalCanaryProbe));
  if(externalCanaryProbe==1) {
    houji_ufs_checkpoint("before XNU GMU external memory positive/negative canary",0);
    if(!houjiGpuExternalCanaryProbeOnce())
      panic("HOUJI GMU DMA: completion or isolation uncertain; retain every BO/table; PSCI-only recovery without later filesystem save");
  }
  int firmwareMemoryProbe=0;PE_parse_boot_argn("houji_gpu_fw_mem_probe",&firmwareMemoryProbe,sizeof(firmwareMemoryProbe));
  if(firmwareMemoryProbe==1) {
    houji_ufs_checkpoint("before XNU inactive firmware/HFI CPU memory preparation",0);
    if(!houjiGpuFirmwareMemoryProbeOnce()) {
      // This probe has no device references, MMIO, firmware execution or DMA.
      // Retained/quarantined CPU BOs cannot own an in-flight device transfer.
      // Commit the refusal report before PSCI; active GPU failures still never
      // take this path or save a filesystem checkpoint.
      houji_ufs_checkpoint("inactive firmware CPU preparation refused; no device publication",0);
      panic("HOUJI FW MEMORY: CPU preparation unqualified; BOs retained; inactive refusal report saved, PSCI-only recovery");
    }
  }
  int scmProbe=0;PE_parse_boot_argn("houji_gpu_scm_probe",&scmProbe,sizeof(scmProbe));
  if(scmProbe!=0) {
    if(!houjiGpuSecureCxProbeOnce())
      panic("HOUJI SCM: fixed secure-CX prerequisite uncertain; every BO/map retained, prearmed PSCI-only recovery without later filesystem save");
  }
  houjiGpuFullHostProbeOnce(); // mode3 combined original lower pipeline, always terminal
  if(gHoujiLive.retained) {
    // Touch startup can save its preflight log; wait for qualified GPU/UFS coexistence.
    if(!houjiNativeGpuFsAllowed())panic("HOUJI TOUCH: live GPU coexistence not ready");
    houjiTouchStart();
    houjiFwProbeStart();
    return;
  }
  houjiGpuStockFirmwareProbeOnce(); // enabled attempt always terminates via retained no-FS PSCI
  houjiTouchStart();
  houji_ufs_checkpoint("native HFSX block backend registered",3);
}

#endif
