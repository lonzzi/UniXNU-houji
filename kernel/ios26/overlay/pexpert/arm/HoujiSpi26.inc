/* SPDX-License-Identifier: BSD-2-Clause-Patent
 * The existing battery transport owns only IPCC INTID261 and RSC INTID37.
 * Adapt it to the reference's GIC dispatcher, preserving the fixed allowlist
 * and checking the actual distributor configuration before reporting success.
 */
#include "HoujiSpi.h"
struct houji26_spi_slot {
    uint32_t busy;
    void (*handler)(void *);
    void *context;
};
static struct houji26_spi_slot houji26_spi_slots[2];
static void houji26_spi_handler(void *context, uint32_t intid) {
    struct houji26_spi_slot *slot = context;
    (void)intid;
    if (slot->handler) slot->handler(slot->context);
}
int houji_spi_attach(uint32_t intid, int edge, void (*handler)(void *), void *context,
    uint32_t *readback, uint64_t *router) {
    if (readback) *readback = 0;
    if (router) *router = 0;
    if ((intid != 261 && intid != 37) || (edge != 0 && edge != 1) || !handler) return HoujiSpiArgument;
    const ml_topology_cpu_t *boot = ml_get_topology_info()->boot_cpu;
    if (!gicd_base || !boot || !gicr_cpu_base[boot->cpu_id]) return HoujiSpiNotReady;
    // Keep these doorbells on the boot CPU as in the iOS16 port. The ADSP
    // worker can migrate during setup and must validate the same affinity it wrote.
    const uint64_t wanted = boot->phys_id;
    struct houji26_spi_slot *slot = &houji26_spi_slots[intid == 261 ? 0 : 1];
    uint32_t expected = 0;
    if (!__atomic_compare_exchange_n(&slot->busy, &expected, 1, false, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) return HoujiSpiBusy;
    slot->handler = handler; slot->context = context;
    kern_return_t status = gic_spi_attach_routed(intid, edge != 0, houji26_spi_handler, slot, wanted);
    if (status != KERN_SUCCESS) {
        slot->handler = NULL;
        __atomic_store_n(&slot->busy, 0, __ATOMIC_RELEASE);
        return status == KERN_NAME_EXISTS ? HoujiSpiBusy : HoujiSpiNotReady;
    }
    uint32_t observed = (gic_read32(gicd_base + GICD_ICFGR(intid / 16)) >> (intid % 16 * 2)) & 3;
    uint64_t route = *(volatile uint64_t *)(gicd_base + GICD_IROUTER(intid));
    if (readback) *readback = observed;
    if (router) *router = route;
    if (observed != (edge ? 2U : 0U) || route != wanted) {
        printf("HOUJI26 ADSP SPI: INTID=%u config=%u route=%llx expected=%llx\n",
            intid, observed, (unsigned long long)route, (unsigned long long)wanted);
        gic_v3_spi_detach(intid);
        slot->handler = NULL;
        __atomic_store_n(&slot->busy, 0, __ATOMIC_RELEASE);
        return HoujiSpiReadback;
    }
    return HoujiSpiOk;
}
int houji_spi_dispatch(uint32_t intid) {
    return (intid == 261 || intid == 37) ? gic_spi_handle(intid) : 0;
}
uint64_t houji_spi_count(uint32_t intid) {
    return (intid == 261 || intid == 37) ? gic_v3_spi_count(intid) : 0;
}
