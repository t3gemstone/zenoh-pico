/*
 * Copyright (c) 2026 ZettaScale Technology
 * Copyright (c) 2026 T3 Foundation Gemstone Project (www.t3gemstone.org)
 *
 * SPDX-License-Identifier: EPL-2.0 OR Apache-2.0
 *
 * z_scout — zenoh-pico scouting example for TI AM67A Cortex-R5F.
 *
 * Periodically issues a z_scout() to discover zenoh peers and routers
 * reachable on the network, then prints their ZID, whatami, and locators
 * via DebugP_log.
 *
 * z_scout() does NOT open a persistent session — it sends a Scout message
 * and waits for Hello replies during a short timeout window.
 */

#include <stdint.h>
#include <string.h>

#include "ti_drivers_config.h"
#include "ti_drivers_open_close.h"
#include "ti_board_config.h"
#include "ti_board_open_close.h"

#include <kernel/dpl/DebugP.h>

#include "FreeRTOS.h"
#include "task.h"

#include <zenoh-pico.h>
#include "zenoh_net_init.h"

/* ---- Configuration -------------------------------------------------------- */
#define SCOUT_INTERVAL_MS  10000U  /* repeat scout every 10 s */

/* ---- FreeRTOS task parameters --------------------------------------------- */
#define APP_TASK_NAME  "z_scout"
#define APP_TASK_PRI   (configMAX_PRIORITIES - 2)
#define APP_TASK_STACK (8192U / sizeof(configSTACK_DEPTH_TYPE))

static StackType_t  gAppTaskStack[APP_TASK_STACK] __attribute__((aligned(32)));
static StaticTask_t gAppTaskObj;

#define MAIN_TASK_STACK (16384U / sizeof(configSTACK_DEPTH_TYPE))
static StackType_t  gMainTaskStack[MAIN_TASK_STACK] __attribute__((aligned(32)));
static StaticTask_t gMainTaskObj;

/* ---- Scout result state (shared between callback and drop) ---------------- */
static volatile uint32_t gs_scout_count;

/* ---- Hello callback ------------------------------------------------------- */
static void hello_handler(z_loaned_hello_t *hello, void *ctx) {
    (void)ctx;

    gs_scout_count++;

    /* ZID — print all 16 bytes as hex */
    z_id_t zid = z_hello_zid(hello);
    char zid_str[33];
    for (int i = 0; i < 16; i++) {
        snprintf(&zid_str[i * 2], 3, "%02X", (unsigned)zid.id[i]);
    }

    /* whatami string */
    z_view_string_t whatami_str;
    z_whatami_to_view_string(z_hello_whatami(hello), &whatami_str);

    DebugP_log("[z_scout] Hello { zid: %s, whatami: %.*s }\r\n",
               zid_str,
               (int)z_string_len(z_loan(whatami_str)),
               z_string_data(z_loan(whatami_str)));

    /* Locators */
    const z_loaned_string_array_t *locs = zp_hello_locators(hello);
    size_t n = z_string_array_len(locs);
    for (size_t i = 0; i < n; i++) {
        const z_loaned_string_t *loc = z_string_array_get(locs, i);
        DebugP_log("[z_scout]   locator[%zu]: %.*s\r\n",
                   i, (int)z_string_len(loc), z_string_data(loc));
    }
}

static void hello_drop(void *ctx) {
    (void)ctx;
    if (gs_scout_count == 0U) {
        DebugP_log("[z_scout] No zenoh nodes found.\r\n");
    } else {
        DebugP_log("[z_scout] Found %u node(s).\r\n", (unsigned)gs_scout_count);
    }
}

/* ---- Scout task ----------------------------------------------------------- */
static void z_scout_task(void *arg) {
    (void)arg;

    int net_rc = zenoh_net_init();
    if (net_rc != ZENOH_NET_OK) {
        DebugP_log("[z_scout] Network init failed (%d)\r\n", net_rc);
        vTaskDelete(NULL);
        return;
    }

    char ip_str[16];
    zenoh_net_ip_str(ip_str, sizeof(ip_str));
    DebugP_log("[z_scout] IP: %s  interval: %u ms\r\n",
               ip_str, (unsigned)SCOUT_INTERVAL_MS);

    while (1) {
        DebugP_log("[z_scout] Scouting...\r\n");
        gs_scout_count = 0U;

        z_owned_config_t cfg;
        z_config_default(&cfg);

        z_owned_closure_hello_t closure;
        z_closure(&closure, hello_handler, hello_drop, NULL);

        z_scout(z_move(cfg), z_move(closure), NULL);

        vTaskDelay(pdMS_TO_TICKS(SCOUT_INTERVAL_MS));
    }

    zenoh_net_deinit();
    vTaskDelete(NULL);
}

/* ---- FreeRTOS entry ------------------------------------------------------- */
void freertos_main(void *arg) {
    (void)arg;
    Drivers_open();
    Board_driversOpen();
    xTaskCreateStatic(z_scout_task, APP_TASK_NAME, APP_TASK_STACK,
                      NULL, APP_TASK_PRI, gAppTaskStack, &gAppTaskObj);
    vTaskDelete(NULL);
}

int main(void) {
    System_init();
    Board_init();
    xTaskCreateStatic(freertos_main, "freertos_main", MAIN_TASK_STACK,
                      NULL, configMAX_PRIORITIES - 1,
                      gMainTaskStack, &gMainTaskObj);
    vTaskStartScheduler();
    DebugP_assertNoLog(0);
    return 0;
}
