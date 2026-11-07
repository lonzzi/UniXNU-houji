/* SPDX-License-Identifier: BSD-2-Clause-Patent */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

typedef int kern_return_t;
enum { KERN_SUCCESS, KERN_NAME_EXISTS };
typedef struct { unsigned cpu_id; uint32_t phys_id; } ml_topology_cpu_t;
typedef struct { ml_topology_cpu_t *boot_cpu; } ml_topology_info_t;
static ml_topology_cpu_t boot = {0, 0x100};
static ml_topology_info_t topology = {&boot};
static const ml_topology_info_t *ml_get_topology_info(void) { return &topology; }

#define GICD_ICFGR(n) (0xc00 + 4 * (n))
#define GICD_IROUTER(n) (0x6000 + 8 * (n))
static uint64_t registers[0x10000 / 8];
static uintptr_t gicd_base, gicr_cpu_base[8];
static uint32_t caller_affinity;
static unsigned attach_calls, detach_calls, deliveries;
static bool wrong_route;
static uint32_t gic_read32(uintptr_t addr) {
    uint32_t value;
    memcpy(&value, (void *)addr, sizeof(value));
    return value;
}
static kern_return_t gic_spi_attach_routed(uint32_t intid, bool edge,
    void (*handler)(void *, uint32_t), void *context, uint64_t route) {
    ++attach_calls;
    assert(!edge && route == boot.phys_id && caller_affinity != route);
    caller_affinity += 0x100; // The worker migrated after the route was chosen.
    registers[GICD_IROUTER(intid) / 8] = wrong_route ? caller_affinity : route;
    handler(context, intid);
    return KERN_SUCCESS;
}
static void gic_v3_spi_detach(uint32_t intid) {
    assert(intid == 37);
    ++detach_calls;
}
static int gic_spi_handle(uint32_t intid) { return intid == 261 || intid == 37; }
static uint64_t gic_v3_spi_count(uint32_t intid) { return gic_spi_handle(intid); }

#include "../kernel/ios26/overlay/pexpert/arm/HoujiSpi26.inc"

static void callback(void *context) {
    assert(context == &deliveries);
    ++deliveries;
}

int main(void) {
    uint32_t config = 1;
    uint64_t route = 1;
    gicd_base = (uintptr_t)registers;
    caller_affinity = 0x300;
    assert(houji_spi_attach(261, 0, callback, &deliveries, &config, &route) == HoujiSpiNotReady);
    assert(config == 0 && route == 0 && attach_calls == 0);
    gicr_cpu_base[0] = 1;
    assert(houji_spi_attach(42, 0, callback, &deliveries, NULL, NULL) == HoujiSpiArgument);
    assert(houji_spi_attach(261, 0, callback, &deliveries, &config, &route) == HoujiSpiOk);
    assert(config == 0 && route == boot.phys_id && caller_affinity == 0x400);
    assert(houji_spi_attach(261, 0, callback, &deliveries, NULL, NULL) == HoujiSpiBusy);
    wrong_route = true;
    assert(houji_spi_attach(37, 0, callback, &deliveries, &config, &route) == HoujiSpiReadback);
    assert(detach_calls == 1 && route == caller_affinity);
    wrong_route = false;
    assert(houji_spi_attach(37, 0, callback, &deliveries, &config, &route) == HoujiSpiOk);
    assert(attach_calls == 3 && route == boot.phys_id && deliveries == 3);
    puts("PASS: iOS26 ADSP SPIs retain boot affinity across caller migration and reject a bad readback");
}
