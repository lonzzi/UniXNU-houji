// SPDX-License-Identifier: MIT
// Houji Metal discovery: explicit NativeAdrenoMetal.clients selects backboardd
// and its listed clients. The frontend uses the matching metal2vulkan/Turnip
// stack over the A750 kernel transport. Discovery never runs an A740 raw-PM4
// self-test and does not require a .publish marker on the houji image.
#import <Foundation/Foundation.h>
#include "SharedShaderCache.h"
#include <pthread.h>
#include <algorithm>
#include <thread>
#include <dlfcn.h>
#if __has_feature(ptrauth_calls)
#include <ptrauth.h>
#endif
#include <unistd.h>
#include <pwd.h>
#include <fts.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <time.h>
#include <mach/mach.h>
#include <sys/sysctl.h>
#include <libkern/OSCacheControl.h>
#include <objc/message.h>
#include "NativeAdrenoUserTest.h"
#include "NativeMetalBridgePhase.h"
#include "CacheExport.h"
#include "PrimeShaders.h"
#include "ExceptionDiag.h"
#include "NativeTurnipPolicy.h"

@interface _MTLDevice : NSObject
@end
@interface NativeAdrenoMetalDeviceBase : NSObject   // only a lock object for the published mode's one-time setup
@end
@implementation NativeAdrenoMetalDeviceBase
@end
@interface NativeAdrenoMetalDevice : _MTLDevice
- (id)initWithAcceleratorPort:(unsigned)port;
@end

// ---- published mode ----
namespace publish {
// The image's marker: a file beside the bundle in the union view of /System/Library/Extensions (the bundle's own seal stays untouched).
// A file beside the bundle: its path in path, true when it is readable.
static bool requestedPath(const char *name, char *path, size_t size) {
    Dl_info info = {};
    if (!dladdr(reinterpret_cast<const void *>(&requestedPath), &info) || !info.dli_fname) return false;
    snprintf(path, size, "%s", info.dli_fname);
    char *slash = strrchr(path, '/');          // the bundle's executable: <dir>/NativeAdrenoMetal.bundle/NativeAdrenoMetal
    if (!slash) return false;
    *slash = 0;
    slash = strrchr(path, '/');
    if (!slash) return false;
    snprintf(slash + 1, size - size_t(slash + 1 - path), "%s", name);
    return access(path, R_OK) == 0;
}

static Gpu *makeGpu(unsigned port) {
    void *iokit = dlopen("/System/Library/Frameworks/IOKit.framework/IOKit", RTLD_NOW | RTLD_LOCAL);
    if (!iokit) return nullptr;
    Gpu *gpu = new Gpu;
    gpu->call = (CallFn)dlsym(iokit, "IOConnectCallStructMethod");
    gpu->map = (MapFn)dlsym(iokit, "IOConnectMapMemory64");
    gpu->unmap = (UnmapFn)dlsym(iokit, "IOConnectUnmapMemory64");
    gpu->openFn = (IOServiceOpenFn)dlsym(iokit, "IOServiceOpen");
    gpu->closeFn = (IOServiceCloseFn)dlsym(iokit, "IOServiceClose");
    gpu->port = port;
    return gpu;
}
static std::string gCacheDirectory;   // the runtime's cache directory (the one that opened the device), exported by CacheExport.h
// Progress markers 0x68xx (GPU6): 6800 entered, 6801 Turnip found, 6802 device made, 6803 returned to the discovery, 68f1 no Turnip, 68f2 no device.
static unsigned gDiagBits;
// Reads of the render server's own state for the framebuffer-format question (diagnostic only, for the exact QuartzCore of the target image; a read
// that fails or a build that differs reports zeros). Each value is one marker, 0x7Kvv: K picks the item, vv the value.
static bool bootArgSet(const char *key) {
    char args[1100] = {};
    size_t size = sizeof args - 1;
    return sysctlbyname("kern.bootargs", args, &size, nullptr, 0) == 0 && strstr(args, key);
}
static void reportDisplayState(Gpu &gpu) {(void)gpu;} // iOS16: no offsets from another OS cache.
// Every process other than the render server: the same runtime on its own connection, with none of the render server's threads (no
// display hand-off, telemetry, benchmark or cache export). Its shader cache lives in its own cache or temp directory.
// The reason a client's device failed, as a text line the driver keeps per process (the render server's report row is rewritten every ten seconds):
// "<step> <detail>", through the marker stream (a connection is not needed). The Gpu object is made only on this path.
// Deletes every "*.import" file under `root`: the copying import of an earlier image wrote each file beside its place and renamed it only
// once the whole pack was read; a boot that ended first left them all there.
static void removeHalfImported(const char *root) {
    char *paths[] = {const_cast<char *>(root), nullptr};
    FTS *fts = fts_open(paths, FTS_PHYSICAL | FTS_NOCHDIR | FTS_XDEV, nullptr);
    if (!fts) return;
    while (FTSENT *e = fts_read(fts))
        if (e->fts_info == FTS_F && e->fts_namelen > 7 && !strcmp(e->fts_name + e->fts_namelen - 7, ".import")) unlink(e->fts_path);
    fts_close(fts);
}
// Bounded startup evidence survives a refused device before its pipeline journal opens.
static char gStartupDirectory[512];
static unsigned gStartupRows;
static void startupDirectory(const char *path) {
    if(gStartupDirectory[0] || !path || !*path)return;
    struct stat st={};
    if(!lstat(path,&st) && S_ISDIR(st.st_mode) && st.st_uid==getuid() && !(st.st_mode&0077))
        strlcpy(gStartupDirectory,path,sizeof gStartupDirectory);
}
static void startupLog(const char *step,const char *detail) {
    if(HOUJI_METAL_QUIET)return;
    if(!gStartupDirectory[0] || __sync_fetch_and_add(&gStartupRows,1)>=32)return;
    char path[640];snprintf(path,sizeof path,"%s/initialization.log",gStartupDirectory);
    struct stat st={};
    if(!lstat(path,&st) && (!S_ISREG(st.st_mode) || st.st_uid!=getuid() || (st.st_mode&0077) || st.st_size>=24576))return;
    int fd=open(path,O_WRONLY|O_APPEND|O_CREAT|O_NOFOLLOW,0600);
    if(fd<0)return;
    char line[768];const int n=snprintf(line,sizeof line,"pid=%d process=%s step=%.40s detail=%.600s\n",getpid(),getprogname(),step,detail?detail:"");
    if(n>0)(void)!write(fd,line,std::min((size_t)n,sizeof line-1));
    close(fd);
}
// ABI v1 has no upstream telemetry-marker transport. Retain bounded snapshots
// in the already verified cache directory; no disk writes on render threads.
static void diagnosticLog(const char *text,size_t length) {
    if(HOUJI_METAL_QUIET)return;
    static unsigned samples;
    if(!gStartupDirectory[0] || !text || !length || samples++>=32)return;
    char path[640];snprintf(path,sizeof path,"%s/telemetry-%d.log",gStartupDirectory,getpid());
    int fd=open(path,O_WRONLY|O_APPEND|O_CREAT|O_NOFOLLOW,0600);
    if(fd<0)return;
    struct stat st={};
    if(fstat(fd,&st) || !S_ISREG(st.st_mode) || st.st_uid!=getuid() || (st.st_mode&0077) || st.st_size>256*1024-6656){close(fd);return;}
    char heading[512];const int n=snprintf(heading,sizeof heading,"pid=%d uptime-us=%llu TU_DEBUG=%.128s\n",getpid(),
        (unsigned long long)(clock_gettime_nsec_np(CLOCK_UPTIME_RAW)/1000),getenv("TU_DEBUG")?:"unset");
    if(n>0)(void)!write(fd,heading,std::min((size_t)n,sizeof heading-1));
    (void)!write(fd,text,std::min(length,(size_t)6144));
    close(fd);
}
static unsigned gClientPort;
static void clientFailure(unsigned port, const char *step, const char *detail) {
    if(HOUJI_METAL_QUIET)return;
    if (Gpu *gpu = makeGpu(port)) {
        // The row holds 63 characters: the common prefix goes, and a long reason keeps its start (what was refused) and its end (why).
        if (!detail) detail = "";
        if (!strncmp(detail, "native-metal: ", 14)) detail += 14;
        char shown[48];
        const size_t length = strlen(detail);
        if (length <= 46) strlcpy(shown, detail, sizeof shown);
        else snprintf(shown, sizeof shown, "%.18s~%s", detail, detail + length - 27);
        char text[64];
        snprintf(text, sizeof text, "%.14s %s", step, shown);
        streamText(*gpu, text);
        // A refusal's text is what says why a client ends (iconservicesagent's pipeline refusal showed 18 characters of it): the rest of it
        // follows in rows of its own, up to six, each 46 characters.
        if (!strcmp(step, "problem") && length > 46)
            for (unsigned part = 1, at = 46; part <= 6 && at < length; ++part, at += 46) {
                // The log keeps the latest text row only: each part follows the one before it by 4 s so a sample catches every one.
                std::string row = "problem" + std::to_string(part) + " " + std::string(detail + at).substr(0, 46);
                auto *target = gpu;
                dispatch_after(dispatch_time(DISPATCH_TIME_NOW, int64_t(part) * 4 * NSEC_PER_SEC),
                               dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{streamText(*target, row.c_str());});
            }
    }
}
// Each process's own render passes, as its Turnip recorded them (NativeBridgeMetalFrontend.mm): the slowest one's size, format and draw
// count and the pass total, on the client's text line every 10 s while the diagnostics are on. A client whose jobs each took 77 ms showed
// only that; which pass and at what size is what says whether the cost is the driver's.
extern "C" size_t nmb_slowest_pass(char *out, size_t size) __attribute__((weak));
static void startPassReporter(unsigned port) {
    if (!getenv("SHENG_DIAG") || !&nmb_slowest_pass) return;
    static std::atomic<bool> started{false};
    if (started.exchange(true)) return;
    Gpu *gpu = makeGpu(port);
    if (!gpu) return;
    std::thread([gpu]{
        pthread_setname_np("pass-reporter");
        for (;;) {
            sleep(5);
            char row[200];
            if (!nmb_slowest_pass(row, sizeof row) || !row[0]) continue;
            streamText(*gpu, row);
        }
    }).detach();
}
static bool listedClient() {
    char path[1024];
    if (!requestedPath("NativeAdrenoMetal.clients", path, sizeof path)) return false;
    FILE *file = fopen(path, "r");
    if (!file) return false;
    char text[1024] = {};
    const size_t length = fread(text, 1, sizeof text - 1, file);
    fclose(file);
    text[length] = 0;
    const char *self = getprogname();
    for (char *save = nullptr, *name = strtok_r(text, " ,\t\r\n", &save); name; name = strtok_r(nullptr, " ,\t\r\n", &save))
        if (!strcmp(name, "*") || !strcmp(name, self)) return true;
    return false;
}

#if HOUJI_IOS26_METAL
static void setPoolBlockLimit() {
    const char *process = getprogname();
    const bool compositor = process && (!strcmp(process, "backboardd") || !strcmp(process, "SpringBoard"));
    // Full-screen 32-bit targets are about 12.3 MiB. The runtime dedicates
    // requests larger than half a block, so keep those targets pooled.
    setenv("M2V_BLOCK_MB", compositor ? "32" : "8", 0);
}
#endif

static id clientDevice(unsigned port) {
    if (!listedClient()) return nil;
#if HOUJI_IOS26_METAL
    setPoolBlockLimit();
#endif
    char turnip[1024];
    if (!turnip::libraryPath(turnip, sizeof turnip) || access(turnip, R_OK) != 0) {clientFailure(port, "no-turnip", ""); return nil;}
    uint64_t registryID = 0;
    if (void *iokit = dlopen("/System/Library/Frameworks/IOKit.framework/IOKit", RTLD_NOW | RTLD_LOCAL)) {
        if (auto retain = (int (*)(unsigned))dlsym(iokit, "IOObjectRetain")) retain(port);
        if (auto entryID = (int (*)(unsigned, uint64_t *))dlsym(iokit, "IORegistryEntryGetRegistryEntryID")) entryID(port, &registryID);
    }
    // IOSurfaces this GPU reads or writes must be plain rows, as in the render server.
    CFPreferencesSetAppValue(CFSTR("CAEnableIOSurfaceCompression"), kCFBooleanFalse, kCFPreferencesCurrentApplication);
    char candidates[3][512] = {};
    if (!confstr(_CS_DARWIN_USER_CACHE_DIR, candidates[0], sizeof candidates[0] - 16)) candidates[0][0] = 0;
    if (!confstr(_CS_DARWIN_USER_TEMP_DIR, candidates[1], sizeof candidates[1] - 16)) candidates[1][0] = 0;
    strlcpy(candidates[2], sharedShaderCacheDirectory().c_str(), sizeof candidates[2]);
    id device = nil;
    const char *lastError = "";
    unsigned usable = 0;
    for (unsigned i : {2u, 0u, 1u}) {
        if (device) break;
        if (!candidates[i][0]) continue;
        if (i != 2) strlcat(candidates[i], "m2v", sizeof candidates[i]);
        mkdir(candidates[i], 0700);
        if (access(candidates[i], W_OK) != 0) continue;
        ++usable;
        @try {
            startupDirectory(candidates[i]);startupLog("open-cache",candidates[i]);
            startupLog("turnip-policy",getenv("TU_DEBUG"));
                device = native_bridge_metal_open(turnip, candidates[i], registryID, nullptr);
                startupLog(device?"device-ready":"device-refused",device?nmb_runtime_build():nmb_last_error());
        } @catch (NSException *exception) {
            device = nil;
            lastError = "exception";
        }
        if (!device && !lastError[0]) lastError = nmb_last_error();
    }
    if (!device && !usable) {
        setenv("METAL2VULKAN_SHADER_CACHE_OPTIONAL", "1", 1);
        const char *path = candidates[0][0] ? candidates[0] : candidates[1][0] ? candidates[1] : "/var/mobile/tmp/m2v";
        @try {
            device = native_bridge_metal_open(turnip, path, registryID, nullptr);
        } @catch (NSException *exception) {
            device = nil;
            lastError = "exception";
        }
        if (!device && !lastError[0]) lastError = nmb_last_error();
    }
    if (!device) clientFailure(port, usable ? "no-device" : "no-cachedir", lastError);
    // The client's first problem, once, on the same text line, sent from the thread that hit it: WidgetRenderer crashed on a nil render
    // encoder with no reason anywhere readable (its own log lines need the plugin's format strings, which the device does not keep), and a
    // once-a-second reader never ran before the crash (i568).
    if (device) {
        gClientPort = port;
        startPassReporter(port);
        native_bridge_metal_problem_hook = [](const char *problem) {clientFailure(gClientPort, "problem", problem);};
        char problem[200] = {};
        if (native_bridge_metal_first_problem(problem, sizeof problem) && problem[0]) clientFailure(port, "problem", problem);
    }
    return device;
}
static id deviceForPort(unsigned port) {
    static id device;
    static bool tried;
    @synchronized([NativeAdrenoMetalDeviceBase class]) {
        if (tried) return device;
        tried = true;
#if HOUJI_IOS26_METAL
        setPoolBlockLimit();
#endif
        if(!HOUJI_METAL_QUIET)exceptiondiag::start(); // diagnostic only
        // No concurrent binning in any Metal process: with it BV ran ahead of BR and the two stopped waiting on each other inside a pass
        // (int-261p, int-262, and in system memory int-265). Without it Turnip's autotune picks tile memory (GMEM) or system memory per
        // pass: i553 forced system memory for every pass, the GPU busy 19 % at 680 MHz. A tile store writes whole 16x4 blocks, which the
        // runtime keeps within a texture's rows (bridge patch 0016).
        if (FILE *flags = fopen("/private/var/ngpu/tu_debug", "r")) {
            char line[128] = {};
            if (fgets(line, sizeof line, flags)) {line[strcspn(line, "\n")] = 0; setenv("TU_DEBUG", line, 1);}
            fclose(flags);
        }
        // sheng-tu-debug=<flags>: TU_DEBUG for every Metal process (an experiment: sysmem renders without GMEM load and store, gmem always with them).
        {
            char args[1100] = {};
            size_t size = sizeof args - 1;
            if (sysctlbyname("kern.bootargs", args, &size, nullptr, 0) == 0)
                if (const char *at = strstr(args, "sheng-tu-debug=")) {
                    char value[64] = {};
                    strlcpy(value, at + 15, sizeof value);
                    value[strcspn(value, " ")] = 0;
                    setenv("TU_DEBUG", value, 1);
                }
        }
        // Every pass renders in system memory with the 8-bit BGRA framebuffer (sheng-fb-bgra=1): measured against tile memory with the swap of
        // GMEM loads and stores fixed (patches/mesa/turnip-gmem-mutable-swap.patch, lock screen 99.5 % equal to the RGhA picture), the GPU time per
        // command buffer was no better (17.8 ms mean and 56.1 ms at p95 in tile memory, 17.1 and 52.1 in system memory), and one home screen frame
        // still came out with red and blue exchanged. sheng-tu-debug= overrides this for experiments.
        houji_turnip::configure();
        // sheng-diag=1 turns on every diagnostic of the GPU stack in this process: the frontend's frame trace, Turnip's pass stamps and
        // submission ring, the runtime's stage ring, and in the render server the status and telemetry lines. Without it none of them runs.
        if(!HOUJI_METAL_QUIET) {
        if (bootArgSet("sheng-diag=1") || bootArgSet("sheng-gpu-trace=1") || bootArgSet("sheng-gpu-pixels=1")) setenv("SHENG_DIAG", "1", 1);
        else unsetenv("SHENG_DIAG");
        if (bootArgSet("sheng-gpu-trace=1")) setenv("SHENG_GPU_TRACE", "1", 1);
        if (bootArgSet("sheng-gpu-pixels=1")) setenv("SHENG_GPU_PIXELS", "1", 1);
        if (bootArgSet("sheng-wallpaper-freeze=1")) setenv("NATIVE_METAL_FREEZE_WALLPAPER", "1", 1);
#if HOUJI_GPU_STAGE_TRACE
        setenv("SHENG_DIAG", "1", 1);
#endif
        // log-gpu=1: the GPU stack's log lines (NSLog, the pipeline journal); off without it.
        if (bootArgSet("log-gpu=1")) setenv("SHENG_LOG_GPU", "1", 1);
        else unsetenv("SHENG_LOG_GPU");
        setenv("NATIVE_METAL_JOURNAL", "1", 1); // bounded pipeline creation records, no CPU sampler
        } else houji_turnip::disableDiagnostics();
        if (strcmp(getprogname(), "backboardd") != 0) return device = clientDevice(port);
        // The discovery releases the accelerator port when it is done; the monitor and the scanout client keep using it.
        if (void *iokit = dlopen("/System/Library/Frameworks/IOKit.framework/IOKit", RTLD_NOW | RTLD_LOCAL)) {
            if (auto retain = (int (*)(unsigned))dlsym(iokit, "IOObjectRetain")) retain(port);
        }
        Gpu *gpu = makeGpu(port);
        if (!gpu) return nil;
        gpu->mark(0x6800);
        char turnip[1024];
        if (!turnip::libraryPath(turnip, sizeof turnip) || access(turnip, R_OK) != 0) {gpu->mark(0x68f1); return nil;}
        gpu->mark(0x6801);
        // The runtime refuses to open without a writable shader-cache directory, and which directories backboardd's sandbox lets it write is not
        // known from here: try the user cache and temp directories, then /tmp and /var/tmp, and use the first that opens a device.
        uint64_t registryID = 0;
        if (void *iokit = dlopen("/System/Library/Frameworks/IOKit.framework/IOKit", RTLD_NOW | RTLD_LOCAL)) {
            if (auto entryID = (int (*)(unsigned, uint64_t *))dlsym(iokit, "IORegistryEntryGetRegistryEntryID")) entryID(port, &registryID);
        }
        // What the render server sees for its framebuffer choice (marker 0x6Axx): bit0 the deep-framebuffer preference is set, bit1 it is false,
        // bit2 MobileGestalt's answer behind the render server's deep flag is true, bit3 it exists, bit4 the compression capability dictionary exists,
        // bit5 the process is not root.
        {
            unsigned bits = 0;
            if (CFTypeRef value = CFPreferencesCopyAppValue(CFSTR("CAEnableDeepFramebuffer"), kCFPreferencesCurrentApplication)) {
                bits |= 1;
                if (CFGetTypeID(value) == CFBooleanGetTypeID() && !CFBooleanGetValue((CFBooleanRef) value)) bits |= 2;
                CFRelease(value);
            }
            if (auto copyAnswer = (CFTypeRef (*)(CFStringRef)) dlsym(RTLD_DEFAULT, "MGCopyAnswer")) {
                if (CFTypeRef deep = copyAnswer(CFSTR("Aixt/MEN2O2B7f+8m4TxUA"))) {
                    bits |= 8;
                    if (CFGetTypeID(deep) == CFBooleanGetTypeID() && CFBooleanGetValue((CFBooleanRef) deep)) bits |= 4;
                    CFRelease(deep);
                }
                if (CFTypeRef caps = copyAnswer(CFSTR("AoKnINTLPoKML3ctoP0AZg"))) {bits |= 16; CFRelease(caps);}
            }
            if (getuid() != 0) bits |= 32;
            gDiagBits = bits;
            gpu->mark(0x6A00 | bits);
        }
        // The render server compresses its framebuffer (an Apple/AGX layout this GPU does not write) unless told not to; the display driver takes plain rows.
        setenv("CA_DISABLE_FRAMEBUFFER_COMPRESSION", "1", 1);

        // The runtime's creation log reports each VkPipeline making's time to the device's sink, which the telemetry's V row sums
        // (sheng-diag=1).
        if (getenv("SHENG_DIAG")) setenv("METAL2VULKAN_LOG_PIPELINES", "1", 0);
        CFPreferencesSetAppValue(CFSTR("CAEnableFramebufferCompression"), kCFBooleanFalse, kCFPreferencesCurrentApplication);
        CFPreferencesSetAppValue(CFSTR("CAEnableIOSurfaceCompression"), kCFBooleanFalse, kCFPreferencesCurrentApplication);
        char candidates[6][512] = {};
        if (!confstr(_CS_DARWIN_USER_CACHE_DIR, candidates[0], sizeof candidates[0] - 16)) candidates[0][0] = 0;
        if (!confstr(_CS_DARWIN_USER_TEMP_DIR, candidates[1], sizeof candidates[1] - 16)) candidates[1][0] = 0;
        strlcpy(candidates[2], "/tmp/", sizeof candidates[2]);
        strlcpy(candidates[3], "/private/var/tmp/", sizeof candidates[3]);
        // The user temp directory of the mobile user is under /var/mobile/tmp, which launchd's boot task dirs_cleaner empties at every boot,
        // so with a persistent Data store the pipelines were still compiled again each boot (V row hit0). Try the user's Library/Caches first
        // (marker 0x6814); the earlier candidates keep their markers.
        if (const passwd *user = getpwuid(getuid()); user && user->pw_dir && user->pw_dir[0]) {
            snprintf(candidates[4], sizeof candidates[4] - 16, "%s/Library/Caches/com.apple.backboardd", user->pw_dir);
            mkdir(candidates[4], 0700);
            strlcat(candidates[4], "/", sizeof candidates[4]);
        }
        strlcpy(candidates[5], sharedShaderCacheDirectory().c_str(), sizeof candidates[5]);
        static const unsigned order[] = {5, 4, 0, 1, 2, 3};
        for (unsigned i : order) {
            if (device) break;
            if (!candidates[i][0]) continue;
            if (i != 5) strlcat(candidates[i], "m2v", sizeof candidates[i]);
            mkdir(candidates[i], 0700);
            if (access(candidates[i], W_OK) != 0) continue;
            // Files an earlier image's copying import left half written here (int 590aaee2), which hold memory of the Data disk.
            {
                static bool once;
                if (!once && i != 5) {once = true; removeHalfImported(candidates[i]);}
            }
            // The image's pipeline pack (an earlier boot's export), into this directory before the runtime reads it. Marker 0x6Cnn: files written
            // (saturated at 0xfe), 0x6Cff the pack was refused.
            if (char pack[1024]; requestedPath("NativeAdrenoMetal.m2vpack", pack, sizeof pack)) {
                const int imported = cacheexport::importPack(pack, candidates[i]);
                if (imported) gpu->mark(0x6C00 | (imported < 0 ? 0xff : imported > 0xfe ? 0xfe : imported));
            }
            @try {
                startupDirectory(candidates[i]);startupLog("open-cache",candidates[i]);
                startupLog("turnip-policy",getenv("TU_DEBUG"));
            device = native_bridge_metal_open(turnip, candidates[i], registryID, nullptr);
            startupLog(device?"device-ready":"device-refused",device?nmb_runtime_build():nmb_last_error());
            } @catch (NSException *exception) {
                device = nil;
            }
            if (device) {gpu->mark(0x6810 + i); gCacheDirectory = candidates[i];}   // which directory served
        }
        if (!device) {
            gpu->mark(0x68f2);
            char text[64];
            snprintf(text, sizeof text, "PUB no device: %.40s", nmb_last_error());
            streamText(*gpu, text);
            return nil;
        }
        gpu->mark(0x6802);
        // The status line (GPU8) every ten seconds (sheng-diag=1), and the display hand-off client on its own thread: both only read or serve,
        // neither is the discovery's.
        pthread_t monitor, scanout;
        pthread_attr_t attributes;
        pthread_attr_init(&attributes);
        pthread_attr_setdetachstate(&attributes, PTHREAD_CREATE_DETACHED);
        pthread_attr_setstacksize(&attributes, 1u << 20);
        const bool diag = getenv("SHENG_DIAG") != nullptr;
        if(!HOUJI_METAL_QUIET)pthread_create(&monitor, &attributes, [](void *arg) -> void * {
            Gpu *g = static_cast<Gpu *>(arg);
            for (;;) {
                sleep(10);
                char status[64], text[64];
                static unsigned phase;
                // status (counters, then the last two refusals' texts), formats; the stream carries 63 characters per line, so a long status goes
                // out in 57-character pieces on consecutive lines (the UFS log keeps every line).
                char full[420];
                native_bridge_metal_status(full, sizeof full);
                const size_t length = strlen(full);
                const unsigned pieces = (unsigned)((length + 56) / 57);
                const unsigned step = phase++ % (1 + (pieces ? (pieces > 7 ? 7 : pieces) : 1));
                if (step == 0) {
                    native_bridge_metal_formats(status, sizeof status);
                    snprintf(text, sizeof text, "PUB %.58s", status);
                } else {
                    snprintf(text, sizeof text, "ST%u %.57s", step - 1, full + (size_t)(step - 1) * 57);
                }
                streamText(*g, text);
                reportDisplayState(*g);
                g->mark(0x6A00 | gDiagBits);   // the diagnostics page lists the last few markers only
            }
            return nullptr;
        }, gpu);
        pthread_t telemetry;
        if (diag) pthread_create(&telemetry, &attributes, [](void *arg) -> void * {
            (void)arg;
            sleep(5);
            // Keep the bridge's existing telemetry in a bounded text journal;
            // houji ABI v1 cannot transport the upstream v2 marker table.
            for (;;) {
                static char table[6144];
                const size_t length=native_bridge_metal_telemetry(table, sizeof table);
                diagnosticLog(table,length);
                sleep(6);
            }
            return nullptr;
        }, gpu);
        pthread_create(&scanout, &attributes, [](void *arg) -> void * {
            Gpu *g = static_cast<Gpu *>(arg);
            sleep(3);
            @try {
                (void)g; // Board display ABI has no GPU scanout handoff.
            } @catch (NSException *exception) {
                g->mark(0x68f3);
            }
            return nullptr;
        }, gpu);
        // The cache directory's export to the UFS log only when NativeAdrenoMetal.cacheexport sits beside the bundle: Data persists across
        // boots now, so the caches no longer need the trip through the host, and once priming has filled the directory, every rescan and
        // export competed with the render server (int-245: the scanning thread at 45% CPU, the sending one at 48%, frames twice as slow).
        char exportPath[1024];
        if (requestedPath("NativeAdrenoMetal.cacheexport", exportPath, sizeof exportPath)) {
            pthread_t exporter;
            pthread_create(&exporter, &attributes, [](void *arg) -> void * {
                cacheexport::run(*static_cast<Gpu *>(arg), gCacheDirectory);
                return nullptr;
            }, gpu);
        }
        // Every Metal library of the OS through the runtime, 90 s after the render server opened the device (PrimeShaders.h); the boots that froze
        // or crashed before three minutes never primed anything. Progress is kept in the Data cache directory, so later boots resume.
        // The translator recurses deeply on large functions: give it the stack of a main thread.
        pthread_attr_setstacksize(&attributes, 8u << 20);
        pthread_t primer;
        if (bootArgSet("houji-gpu-prime=1")) pthread_create(&primer, &attributes, [](void *) -> void * {
                char list[1024];
                prime::run((id<MTLDevice>)device, gCacheDirectory, 90,
                           requestedPath("NativeAdrenoMetal.primelist", list, sizeof list) ? std::string(list) : std::string());
                return nullptr;
            }, nullptr);
        pthread_attr_destroy(&attributes);
        gpu->mark(0x6803);
        return device;
    }
}
}  // namespace publish

@implementation NativeAdrenoMetalDevice
- (id)initWithAcceleratorPort:(unsigned)port {
    self = [super init];
    if (!self || !port) return nil;
    // A750/iOS 16 uses the official frontend only. The upstream default self-test
    // is an A740 raw-PM4 replay and is not a valid A750 discovery operation.
    // The existing clients file explicitly selects consumers, including backboardd.
    // A failed Turnip/backend open returns nil and leaves software rendering available.
    houji_turnip::configure();
    if(!publish::listedClient())return nil;
    if(!strcmp(getprogname(),"backboardd")){
        CFPreferencesSetAppValue(CFSTR("CAEnableDeepFramebuffer"),kCFBooleanFalse,kCFPreferencesCurrentApplication);
        CFPreferencesSetAppValue(CFSTR("CAEnableFramebufferCompression"),kCFBooleanFalse,kCFPreferencesCurrentApplication);
        return publish::deviceForPort(port);
    }
    return publish::clientDevice(port);
}
@end
