/* SPDX-License-Identifier: APSL-2.0
 * Houji's DSI0/n3 panel and CoreBrightness service. Hardware transactions
 * run on their own timer/work loop; latest request wins during a fade.
 * OS interface references: UniXNU-sheng AppleARMBacklight.cpp and
 * data-a-live 0137/0139/0263. See docs/upstream-features.md for revisions.
 */
#include <libkern/c++/OSNumber.h>
#include <libkern/c++/OSDictionary.h>
#include <libkern/c++/OSCollectionIterator.h>
// Cached facts survive message-ring wrap. Snapshot readers never touch DSI
// or acquire the backlight work loop, including during recovery shutdown.
namespace HoujiBacklightTrace {
enum Field { Stage, Failure, Registered, Requests, Rejected, Grants, Conforms,
    Requested, Applied, Measured, Control, Status, Interrupt, Fifo, ReadCount, Packet,
    AckError, RequestFields, BrightnessKind, FadeKind, RawBrightnessLo,
    RawBrightnessHi, RawFadeLo, RawFadeHi, Count };
static unsigned values[Count];
static void set(Field field,unsigned value){__atomic_store_n(&values[field],value,__ATOMIC_RELAXED);}
static void bump(Field field){__atomic_fetch_add(&values[field],1u,__ATOMIC_RELAXED);}
}
extern "C" unsigned houji_backlight_snapshot(char *out,unsigned capacity) {
    if(!out || !capacity)return 0;
    using namespace HoujiBacklightTrace;
    unsigned v[Count];for(unsigned i=0;i<Count;++i)v[i]=__atomic_load_n(&values[i],__ATOMIC_RELAXED);
    int n=snprintf(out,capacity,
        "HOUJI BACKLIGHT SNAPSHOT: stage=%u failure=%u registered=%u requests=%u rejected=%u grants=%u conforms=%u requested=%u last-level=%u boot-measured=%u\n"
        "HOUJI BACKLIGHT DSI: ctrl=%x status=%x irq=%x fifo=%x read-count=%x packet=%x ack-error=%x; cached observations\n"
        "HOUJI BACKLIGHT INPUT: fields=%u brightness-kind=%u raw=%x:%x fade-kind=%u raw=%x:%x; kind 1=number 2=value-dictionary 3=other 4=nits\n",
        v[Stage],v[Failure],v[Registered],v[Requests],v[Rejected],v[Grants],v[Conforms],v[Requested],v[Applied],v[Measured],
        v[Control],v[Status],v[Interrupt],v[Fifo],v[ReadCount],v[Packet],v[AckError],
        v[RequestFields],v[BrightnessKind],v[RawBrightnessHi],v[RawBrightnessLo],
        v[FadeKind],v[RawFadeHi],v[RawFadeLo]);
    return n>0?((unsigned)n<capacity?(unsigned)n:capacity-1):0;
}
#include "HoujiBacklightProtocol.h"
#if HOUJI_IOS26
static bool houji_backlight_publish_ios26(IOService *service) {
    // CoreBrightness 23G90 needs both properties before matching the service.
    // These are logical slider units, based on its 5/140/400 fallback range.
    // The existing DSI policy still converts the normalized request to panel levels.
    static constexpr uint32_t curve[] = {5u * HBL_SCALE, 140u * HBL_SCALE, HBL_IOS26_MAX_NITS * HBL_SCALE};
    static constexpr struct { const char *key; uint32_t value; } calibration[] = {
        {"current-for-max-backlight", HBL_SCALE},
        {"current-for-mid-backlight", HBL_SCALE / 2},
        {"hardware-max-current-limit", HBL_SCALE},
        {"lMaxProduct", curve[2]}, {"lMidProduct", curve[1]}, {"lMinProduct", curve[0]},
        {"lMaxPanel", curve[2]}, {"lMidPanel", curve[1]}, {"lMinPanel", curve[0]},
    };
    OSDictionary *parameters = OSDictionary::withCapacity(sizeof(calibration) / sizeof(calibration[0]));
    OSData *table = OSData::withBytes(curve, sizeof(curve));
    bool complete = parameters && table;
    for (const auto &entry : calibration) {
        if (!complete) break;
        OSNumber *number = OSNumber::withNumber(entry.value, 32);
        complete = number && parameters->setObject(entry.key, number);
        OSSafeReleaseNULL(number);
    }
    if (complete) {
        complete = service->setProperty("backlight-calibration-parameters", parameters) &&
            service->setProperty("backlight-marketing-table", table);
    }
    OSSafeReleaseNULL(parameters);
    OSSafeReleaseNULL(table);
    return complete;
}
#endif
class HoujiBacklight : public IOService {
    OSDeclareDefaultStructors(HoujiBacklight)
    IOWorkLoop *loop=nullptr;
    IOTimerEventSource *timer=nullptr;
    IOMemoryMap *map=nullptr;
    uintptr_t base=0;
    HoujiDsiCommand command={};
    uint64_t request=0,fadeStart=0,fadeDuration=0,readbackDeadline=0;
    unsigned from=0,to=0,current=0,written=~0u,sending=0,stage=0;
    bool failed=false;
    void observe() {
        using namespace HoujiBacklightTrace;
        set(Stage,stage);
        if(!base)return;
        set(Control,read(this,4));set(Status,read(this,8));
        set(Interrupt,read(this,0x110));set(Fifo,read(this,0x15c));
        set(ReadCount,read(this,0x1d4));set(Packet,read(this,0x6c));set(AckError,read(this,0x68));
    }
    // Failure codes: map/mode=1, workloop=2, maximum-return command=3,
    // command completion=4, read submission=5, response timeout/error=6,
    // brightness submission=7, iOS26 profile allocation=8.
    // Do not retry an uncertain FIFO transaction.
    void fail(unsigned reason) {
        observe();HoujiBacklightTrace::set(HoujiBacklightTrace::Failure,reason);
        __atomic_store_n(&failed,true,__ATOMIC_RELEASE);
        setProperty("BacklightWriteFailed",true);
    }
    static uint64_t now(){uint64_t ns=0;absolutetime_to_nanoseconds(mach_absolute_time(),&ns);return ns/1000;}
    static uint32_t read(void *ctx,unsigned off) {
        auto *s=static_cast<HoujiBacklight *>(ctx);
        uint32_t v=*(volatile uint32_t *)(s->base+off);
        __asm__ volatile("dmb oshld" ::: "memory");return v;
    }
    static void write(void *ctx,unsigned off,uint32_t v) {
        auto *s=static_cast<HoujiBacklight *>(ctx);
        __asm__ volatile("dmb oshst" ::: "memory");
        *(volatile uint32_t *)(s->base+off)=v;
        __asm__ volatile("dsb osh" ::: "memory");
    }
    void publish(unsigned value) {
        OSDictionary *b=OSDictionary::withCapacity(3),*p=OSDictionary::withCapacity(1);
        OSNumber *min=OSNumber::withNumber(0ULL,32),*max=OSNumber::withNumber((uint64_t)HBL_SCALE,32),*v=OSNumber::withNumber(value,32);
        if(b && p && min && max && v){b->setObject("min",min);b->setObject("max",max);b->setObject("value",v);p->setObject("brightness",b);setProperty("IODisplayParameters",p);}
        OSSafeReleaseNULL(b);OSSafeReleaseNULL(p);OSSafeReleaseNULL(min);OSSafeReleaseNULL(max);OSSafeReleaseNULL(v);
    }
    void publishFirst(unsigned level,bool measured) {
#if HOUJI_IOS26
        if (!houji_backlight_publish_ios26(this)) {
            fail(8);
            IOLog("HOUJI BACKLIGHT: CoreBrightness profile unavailable\n");
            return;
        }
#endif
        current=from=to=houjiBacklightFraction(level);written=level;
        publish(current);setProperty("BootBrightnessMeasured",measured);
        setProperty("backlight-control",true);registerService();
        HoujiBacklightTrace::set(HoujiBacklightTrace::Registered,1);
        HoujiBacklightTrace::set(HoujiBacklightTrace::Measured,measured);
        HoujiBacklightTrace::set(HoujiBacklightTrace::Applied,level);observe();
        IOLog("HOUJI BACKLIGHT: n3 DSI0, normal range 0..%u, initial=%u measured=%u\n",HBL_MAX,level,measured);
    }
    static void tick(OSObject *owner,IOTimerEventSource *) {
        auto *s=static_cast<HoujiBacklight *>(owner);uint64_t t=now();
        if(__atomic_load_n(&s->failed,__ATOMIC_ACQUIRE))return;
        if(s->stage==4){
            uint32_t control=read(s,0x1d4);unsigned level=0;
            if((read(s,0x110)&(1u<<24)) || ((control>>16)<4 && t>=s->readbackDeadline)) {
                s->fail(6);
                IOLog("HOUJI BACKLIGHT: panel readback failed; further writes disabled\n");return;
            }
            if((control>>16)<4){s->timer->setTimeoutMS(1);return;}
            bool valid=houjiBacklightReadback(control,read(s,0x6c),&level);
            s->stage=3;s->publishFirst(valid?level:307,valid);return;
        }
        if(s->command.pending) {
            int result=s->command.poll(t);
            if(!result){s->timer->setTimeoutMS(1);return;}
            if(result<0){s->fail(4);IOLog("HOUJI BACKLIGHT: DSI command failed; further writes disabled\n");return;}
            if(s->stage==1){
                s->stage=2;
                if(!s->command.readBrightness(t)){s->fail(5);return;}
                s->observe();
                s->timer->setTimeoutMS(1);return;
            }
            if(s->stage==2){
                // Wait for a fresh complete packet, not a fixed BTA latency.
                s->stage=4;s->observe();s->readbackDeadline=t+50000;s->timer->setTimeoutMS(1);return;
            } else {s->written=s->sending;s->setProperty("AppliedPanelLevel",s->written,32);HoujiBacklightTrace::set(HoujiBacklightTrace::Applied,s->written);}
        }
        uint64_t r=__atomic_exchange_n(&s->request,0ull,__ATOMIC_ACQ_REL);
        if(r>>63){s->from=s->current;s->to=(unsigned)((r>>32)&0x1ffff);s->fadeStart=t;s->fadeDuration=(uint32_t)r;}
        s->current=houjiBacklightFade(s->from,s->to,t-s->fadeStart,s->fadeDuration);
        unsigned level=houjiBacklightLevel(s->current);
        if(level!=s->written) {
            s->sending=level;
            if(!s->command.brightness(level,t)){s->fail(7);return;}
            s->timer->setTimeoutMS(1);return;
        }
        if(s->current!=s->to || __atomic_load_n(&s->request,__ATOMIC_ACQUIRE))s->timer->setTimeoutMS(16);
    }
public:
    static OSNumber *numberValue(OSObject *object,unsigned *kind) {
        *kind=object?3:0;
        if(auto *n=OSDynamicCast(OSNumber,object)){*kind=1;return n;}
        if(auto *d=OSDynamicCast(OSDictionary,object)) {
            if(auto *n=OSDynamicCast(OSNumber,d->getObject("value"))){*kind=2;return n;}
        }
        return nullptr;
    }
    static bool values(OSObject *object,unsigned *level,uint64_t *duration) {
        using namespace HoujiBacklightTrace;
        auto *d=OSDynamicCast(OSDictionary,object);
        set(RequestFields,d?d->getCount():0);
        unsigned bk=0,fk=0;
        auto *legacy=d?d->getObject("brightness"):nullptr;
        auto *b=numberValue(legacy,&bk);
        uint32_t scale=1;
#if HOUJI_IOS26
        // CoreBrightness 23G90 sends signed 16.16 nits to this service.
        // The published calibration defines the maximum for normalization.
        if(auto *nits=d?d->getObject("brightness-nits"):nullptr) {
            if(legacy)return false;
            b=OSDynamicCast(OSNumber,nits);
            bk=b?4:3;
            scale=HBL_IOS26_MAX_NITS;
        }
#endif
        auto *f=numberValue(d?d->getObject("FadePeriod"):nullptr,&fk);
        uint64_t raw=b?b->unsigned64BitValue():0,fade=f?f->unsigned64BitValue():0;
        set(BrightnessKind,bk);set(FadeKind,fk);
        set(RawBrightnessLo,(unsigned)raw);set(RawBrightnessHi,(unsigned)(raw>>32));
        set(RawFadeLo,(unsigned)fade);set(RawFadeHi,(unsigned)(fade>>32));
        // CoreBrightness may send a parameter dictionary's value and unrelated
        // display metadata alongside brightness. Only these two numeric keys
        // affect this service; never forward the dictionary to setProperties
        // on a superclass or copy arbitrary keys into the registry.
        if(!d || d->getCount()>16 || !b || raw>(uint64_t)HBL_SCALE*scale || (fk && !f))return false;
        if(fade>10u*65536u)return false;
        *level=(unsigned)((raw+scale/2)/scale);*duration=fade*1000000/65536;return true;
    }
    bool start(IOService *provider) APPLE_KEXT_OVERRIDE {
        if(!IOService::start(provider))return false;
        IODeviceMemory *m=IODeviceMemory::withRange(0x0ae94000ULL,0x1000);
        map=m?m->map(kIOMapAnywhere|kIOMapInhibitCache):nullptr;if(m)m->release();
        base=map?map->getVirtualAddress():0;
        if(!base || (read(this,4)&5)!=5){fail(1);return false;}
        loop=IOWorkLoop::workLoop();timer=IOTimerEventSource::timerEventSource(this,&HoujiBacklight::tick);
        if(!loop || !timer || loop->addEventSource(timer)!=kIOReturnSuccess){fail(2);return false;}
        command.io={this,read,write};stage=1;uint32_t word=0x80370002u;
        if(!command.begin(&word,1,now())){fail(3);return false;}
        observe();
        timer->setTimeoutMS(1);return true;
    }
    IOReturn setProperties(OSObject *object) APPLE_KEXT_OVERRIDE {
        unsigned level=0;uint64_t duration=0;
        HoujiBacklightTrace::bump(HoujiBacklightTrace::Requests);
        if(!values(object,&level,&duration)){HoujiBacklightTrace::bump(HoujiBacklightTrace::Rejected);return kIOReturnBadArgument;}
        if(__atomic_load_n(&failed,__ATOMIC_ACQUIRE)){HoujiBacklightTrace::bump(HoujiBacklightTrace::Rejected);return kIOReturnNotReady;}
        HoujiBacklightTrace::set(HoujiBacklightTrace::Requested,level);
        __atomic_store_n(&request,(1ull<<63)|((uint64_t)level<<32)|duration,__ATOMIC_RELEASE);
        publish(level);timer->setTimeoutUS(1);return kIOReturnSuccess;
    }
    void stop(IOService *provider) APPLE_KEXT_OVERRIDE {
        __atomic_store_n(&failed,true,__ATOMIC_RELEASE);if(timer){timer->cancelTimeout();timer->disable();if(loop)loop->removeEventSource(timer);}IOService::stop(provider);
    }
    void free() APPLE_KEXT_OVERRIDE {
        OSSafeReleaseNULL(timer);OSSafeReleaseNULL(loop);OSSafeReleaseNULL(map);IOService::free();
    }
};
OSDefineMetaClassAndStructors(HoujiBacklight,IOService)
static HoujiBacklight *houjiBacklightService;
// Only the user-space compatibility query is extended. The kernel retains
// the real metaclass; no copied vtable or unsafe AppleARMBacklight downcast.
extern "C" bool houji_backlight_conforms(const OSObject *object,const char *name) {
    bool matches=object && object==houjiBacklightService && !strcmp(name,"AppleARMBacklight");
    if(matches)HoujiBacklightTrace::bump(HoujiBacklightTrace::Conforms);
    return matches;
}
extern "C" bool houji_backlight_allows_properties(IORegistryEntry *entry,OSObject *object) {
    if(!entry || entry!=houjiBacklightService)return false;
    char process[32]={};proc_selfname(process,sizeof(process));
    unsigned level=0;uint64_t duration=0;
    bool allowed=!strcmp(process,"backboardd") && HoujiBacklight::values(object,&level,&duration);
    if(allowed)HoujiBacklightTrace::bump(HoujiBacklightTrace::Grants);
    else HoujiBacklightTrace::bump(HoujiBacklightTrace::Rejected);
    return allowed;
}
static void houji_backlight_start(IOService *provider) {
    unsigned enabled=1;PE_parse_boot_argn("houji_bl",&enabled,sizeof(enabled));if(!enabled || houjiBacklightService)return;
    auto *s=new HoujiBacklight;
    if(!s)return;
    if(!s->init()){s->release();return;}
    if(!s->attach(provider)){s->release();return;}
    houjiBacklightService=s;
    if(!s->start(provider)){houjiBacklightService=nullptr;s->stop(provider);s->detach(provider);s->release();IOLog("HOUJI BACKLIGHT: command-mode handoff unavailable\n");return;}
    s->release();
}
