/* SPDX-License-Identifier: MIT
 * One-shot system-app registration after offline staged-bundle migration.
 * Uses the OS rebuild API also called by Procursus uicache -a -f. It leaves
 * user registrations, data, icon layout, capability tags and signatures alone.
 */
#import <Foundation/Foundation.h>
#import <objc/message.h>
#include <dlfcn.h>
#include <stdio.h>
#include <unistd.h>
#include <notify.h>
#include "SystemAppsManifest.h"

#if HOUJI_SYSTEM_APPS_IOS26
#include <fcntl.h>
#include <stdarg.h>
#include <string.h>
#include <sys/sysctl.h>

@interface NSObject (HoujiLaunchServicesRecords)
- (instancetype)initWithIdentifier:(NSString *)identifier platform:(uint32_t)platform error:(NSError **)error;
- (instancetype)initWithBundleIdentifier:(NSString *)identifier error:(NSError **)error;
@end

static BOOL diagnostics;

static BOOL wallpaperDiagnostics(void) {
    char arguments[2048] = {0};
    size_t length = sizeof(arguments) - 1;
    if (sysctlbyname("kern.bootargs", arguments, &length, NULL, 0) != 0) return NO;
    char *save;
    for (char *argument = strtok_r(arguments, " ", &save); argument; argument = strtok_r(NULL, " ", &save))
        if (!strcmp(argument, "houji_wallpaper_diag=1")) return YES;
    return NO;
}

static void appDiagnostic(NSString *format, ...) NS_FORMAT_FUNCTION(1, 2);
static void appDiagnostic(NSString *format, ...) {
    if (!diagnostics) return;
    va_list arguments;
    va_start(arguments, format);
    NSString *message = [[NSString alloc] initWithFormat:format arguments:arguments];
    va_end(arguments);
    NSString *line = [NSString stringWithFormat:@"redwood26: App registrar: %@\n", message];
    NSData *data = [line dataUsingEncoding:NSUTF8StringEncoding];
    if (data.length > 160) return;
    int fd = open("/dev/console", O_WRONLY | O_NONBLOCK | O_NOCTTY | O_CLOEXEC);
    if (fd >= 0) {
        (void)write(fd, data.bytes, data.length);
        close(fd);
    }
}

static void reportPosterRecords(void) {
    if (!diagnostics) return;
    Class pointClass = NSClassFromString(@"LSExtensionPointRecord");
    Class recordClass = NSClassFromString(@"LSApplicationExtensionRecord");
    SEL pointInit = NSSelectorFromString(@"initWithIdentifier:platform:error:");
    SEL recordInit = NSSelectorFromString(@"initWithBundleIdentifier:error:");
    if (![pointClass instancesRespondToSelector:pointInit] ||
        ![recordClass instancesRespondToSelector:recordInit]) {
        appDiagnostic(@"poster records API unavailable");
        return;
    }
    NSError *pointError = nil;
    id point = [[pointClass alloc] initWithIdentifier:@"com.apple.posterkit.provider" platform:2 error:&pointError];
    unsigned found = 0;
    NSError *recordError = nil;
    for (NSString *identifier in @[@"com.apple.MercuryPoster", @"com.apple.PaperBoard.LegacyPoster",
                                   @"com.apple.WallpaperKit.CollectionsPoster"]) {
        NSError *error = nil;
        id record = [[recordClass alloc] initWithBundleIdentifier:identifier error:&error];
        if (record) ++found;
        else if (!recordError) recordError = error;
    }
    appDiagnostic(@"poster point=%d %@/%ld records=%u/3 %@/%ld", point != nil,
                  pointError.domain ?: @"-", (long)pointError.code, found,
                  recordError.domain ?: @"-", (long)recordError.code);
}
#else
#define appDiagnostic(...) ((void)0)
#define reportPosterRecords() ((void)0)
#endif

static FILE *logFile;
static void note(NSString *text) {
    if (!logFile) return;
    flockfile(logFile);
    fprintf(logFile, "%.3f %s\n", NSProcessInfo.processInfo.systemUptime, text.UTF8String);
    fflush(logFile); fsync(fileno(logFile)); funlockfile(logFile);
}
static id send0(id object, NSString *name) {
    return ((id(*)(id,SEL))objc_msgSend)(object, NSSelectorFromString(name));
}
static id send1(id object, NSString *name, id argument) {
    return ((id(*)(id,SEL,id))objc_msgSend)(object, NSSelectorFromString(name), argument);
}

int main(void) { @autoreleasepool {
#if HOUJI_SYSTEM_APPS_IOS26
    diagnostics = wallpaperDiagnostics();
#endif
    appDiagnostic(@"start uid=%u", getuid());
    NSString *stamp = @"/private/var/mobile/Library/Preferences/moe.houji.system-apps.plist";
    NSDictionary *prior = [NSDictionary dictionaryWithContentsOfFile:stamp];
    if ([prior[@"manifest"] isEqual:kSystemAppsManifest] && [prior[@"complete"] boolValue]) return 0;
    logFile = fopen("/private/var/mobile/Library/Logs/UniXNU-system-apps.log", "w");
#if !HOUJI_SYSTEM_APPS_IOS26
    if (!logFile) return 1;
#endif
    note([NSString stringWithFormat:@"START manifest=%@ uid=%u", kSystemAppsManifest, getuid()]);
    // A stalled system service cannot leave a new permanent startup worker.
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 90ll*NSEC_PER_SEC),
                   dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
        appDiagnostic(@"rebuild timed out");
        note(@"TIMEOUT: system registration did not finish within 90 seconds"); _exit(7);
    });
    sleep(8);
    NSDictionary *expected = expectedSystemApps();
    for (NSString *name in expected) {
        NSString *path = [@"/Applications" stringByAppendingPathComponent:name];
        NSDictionary *info = [NSDictionary dictionaryWithContentsOfFile:[path stringByAppendingPathComponent:@"Info.plist"]];
        if (![info[@"CFBundleIdentifier"] isEqual:expected[name]]) {
            appDiagnostic(@"validated bundle mismatch %@", name);
            note([@"REFUSED bundle identity: " stringByAppendingString:name]); return 2;
        }
    }
    appDiagnostic(@"validated %lu bundle paths", (unsigned long)expected.count);
    if (!dlopen("/System/Library/Frameworks/CoreServices.framework/CoreServices", RTLD_NOW)) {
        note(@"ERROR CoreServices unavailable"); return 3;
    }
    @try {
        Class cls = NSClassFromString(@"LSApplicationWorkspace");
        id workspace = [cls respondsToSelector:NSSelectorFromString(@"defaultWorkspace")] ? send0(cls,@"defaultWorkspace") : nil;
        SEL rebuild = NSSelectorFromString(@"_LSPrivateRebuildApplicationDatabasesForSystemApps:internal:user:");
        if (![workspace respondsToSelector:rebuild]) { note(@"ERROR system rebuild API unavailable"); return 4; }
        note(@"REBUILD system=YES internal=YES user=NO");
        BOOL success = ((BOOL(*)(id,SEL,BOOL,BOOL,BOOL))objc_msgSend)(workspace,rebuild,YES,YES,NO);
        appDiagnostic(@"rebuild returned=%d", success);
        note([NSString stringWithFormat:@"REBUILD returned=%d",success]);
        reportPosterRecords();
        if (!success) return 5;
        unsigned verified = 0;
        Class proxyClass = NSClassFromString(@"LSApplicationProxy");
        SEL lookup = NSSelectorFromString(@"applicationProxyForIdentifier:");
        if (![proxyClass respondsToSelector:lookup]) { note(@"ERROR registration lookup unavailable"); return 6; }
        for (NSString *name in [[expected allKeys] sortedArrayUsingSelector:@selector(compare:)]) {
            NSString *identifier = expected[name];
            id proxy = send1(proxyClass,@"applicationProxyForIdentifier:",identifier);
            NSURL *url = [proxy respondsToSelector:NSSelectorFromString(@"bundleURL")] ? send0(proxy,@"bundleURL") : nil;
            NSString *path = [@"/Applications" stringByAppendingPathComponent:name];
            id state = [proxy respondsToSelector:NSSelectorFromString(@"appState")] ? send0(proxy,@"appState") : nil;
            BOOL valid = [state respondsToSelector:NSSelectorFromString(@"isValid")] &&
                ((BOOL(*)(id,SEL))objc_msgSend)(state,NSSelectorFromString(@"isValid"));
            BOOL matches = [url isKindOfClass:NSURL.class] && [url.path isEqual:path] && valid;
            if (matches) ++verified;
            note([NSString stringWithFormat:@"APP %@ registered=%d valid=%d path=%@",identifier,matches,valid,url.path]);
        }
        note([NSString stringWithFormat:@"FINISHED registered=%u expected=%lu; launches not tested",verified,(unsigned long)expected.count]);
        if (verified != expected.count) return 6;
        NSDictionary *done = @{@"manifest":kSystemAppsManifest,@"complete":@YES,@"registered":@(verified)};
        if (![done writeToFile:stamp atomically:YES]) { note(@"ERROR writing completion marker"); return 8; }
        notify_post("com.apple.mobile.application_installed");
    } @catch (NSException *exception) {
        note([NSString stringWithFormat:@"EXCEPTION %@ %@",exception.name,exception.reason]); return 9;
    }
    return 0;
}}
