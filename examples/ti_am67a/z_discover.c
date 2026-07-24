/*
 * Copyright (c) 2026 ZettaScale Technology
 * Copyright (c) 2026 T3 Foundation Gemstone Project (www.t3gemstone.org)
 *
 * SPDX-License-Identifier: EPL-2.0 OR Apache-2.0
 *
 * z_discover — zenoh-pico continuous network discovery for TI AM67A Cortex-R5F.
 *
 * Periodically issues a z_scout() and compares the results with the previous
 * round to detect nodes joining or leaving the zenoh network.  Each discovered
 * node is classified by its role:
 *
 *   Peer   — another embedded node running in peer mode
 *   Router — a zenoh router (typically running on a Linux/Windows host)
 *   Bridge — a zenoh bridge connecting two separate zenoh networks
 *
 * Sample output (via DebugP_log → UART):
 *   [z_discover] IP: 192.168.1.50  scan interval: 5000 ms
 *   [z_discover] Round 1 — scanning...
 *   [z_discover] ++ NEW   Peer   ZID=A3B2...0000  locator=udp/192.168.1.51:7446
 *   [z_discover] ++ NEW   Router ZID=FF01...0001  locator=tcp/192.168.1.1:7447
 *   [z_discover] Round 1 — 2 node(s) visible
 *   [z_discover] Round 2 — scanning...
 *   [z_discover] -- GONE  Peer   ZID=A3B2...0000
 *   [z_discover] Round 2 — 1 node(s) visible
 *
 * z_scout() does NOT open a persistent session — no CPSW/IP traffic beyond
 * the scout multicast bursts until a node is found.
 */

#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>

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
#define DISCOVER_INTERVAL_MS  5000U   /* scan interval                        */
#define DISCOVER_MAX_NODES    16U     /* max tracked nodes (peer + router + bridge) */

/* ---- FreeRTOS task parameters --------------------------------------------- */
#define APP_TASK_NAME  "z_discover"
#define APP_TASK_PRI   (configMAX_PRIORITIES - 2)
#define APP_TASK_STACK (8192U / sizeof(configSTACK_DEPTH_TYPE))

static StackType_t  gAppTaskStack[APP_TASK_STACK] __attribute__((aligned(32)));
static StaticTask_t gAppTaskObj;

#define MAIN_TASK_STACK (16384U / sizeof(configSTACK_DEPTH_TYPE))
static StackType_t  gMainTaskStack[MAIN_TASK_STACK] __attribute__((aligned(32)));
static StaticTask_t gMainTaskObj;

/* ---- Node record ---------------------------------------------------------- */
typedef struct {
    z_id_t       zid;
    z_whatami_t  whatami;
    char         first_locator[64]; /* first advertised locator, truncated   */
    bool         valid;
} node_info_t;

/* ---- Persistent state ----------------------------------------------------- */
static node_info_t gs_seen[DISCOVER_MAX_NODES];   /* nodes from previous round */
static uint32_t    gs_seen_count;

static node_info_t gs_round[DISCOVER_MAX_NODES];  /* nodes from current round  */
static uint32_t    gs_round_count;

static uint32_t    gs_round_number;

/* ---- Helpers -------------------------------------------------------------- */
static bool zid_eq(const z_id_t *a, const z_id_t *b) {
    return memcmp(a->id, b->id, sizeof(a->id)) == 0;
}

static void zid_to_str(const z_id_t *zid, char *buf, size_t buflen) {
    /* Print up to 8 significant bytes (leading non-zero or first 8) */
    int written = 0;
    for (int i = 0; i < 16 && written + 3 < (int)buflen; i++) {
        written += snprintf(buf + written, buflen - (size_t)written,
                            "%02X", (unsigned)zid->id[i]);
    }
}

static const char *whatami_label(z_whatami_t w) {
    switch (w) {
    case Z_WHATAMI_ROUTER: return "Router";
    case Z_WHATAMI_PEER:   return "Peer  ";
    default:               return "Bridge";
    }
}

static void log_node(const char *prefix, const node_info_t *n) {
    char zid_str[33] = {0};
    zid_to_str(&n->zid, zid_str, sizeof(zid_str));
    DebugP_log("[z_discover] %s %s ZID=%s  locator=%s\r\n",
               prefix,
               whatami_label(n->whatami),
               zid_str,
               n->first_locator[0] ? n->first_locator : "(none)");
}

/* ---- Scout callbacks ------------------------------------------------------ */
static void hello_handler(z_loaned_hello_t *hello, void *ctx) {
    (void)ctx;

    if (gs_round_count >= DISCOVER_MAX_NODES) {
        return;
    }

    node_info_t *n = &gs_round[gs_round_count++];
    n->zid      = z_hello_zid(hello);
    n->whatami  = z_hello_whatami(hello);
    n->valid    = true;
    n->first_locator[0] = '\0';

    /* Copy first advertised locator if available */
    const z_loaned_string_array_t *locs = zp_hello_locators(hello);
    if (z_string_array_len(locs) > 0U) {
        const z_loaned_string_t *loc = z_string_array_get(locs, 0);
        size_t len = z_string_len(loc);
        if (len >= sizeof(n->first_locator)) {
            len = sizeof(n->first_locator) - 1U;
        }
        memcpy(n->first_locator, z_string_data(loc), len);
        n->first_locator[len] = '\0';
    }
}

static void scout_done(void *ctx) {
    (void)ctx;

    /* --- Detect NEW nodes (in round, not in seen) --- */
    for (uint32_t i = 0U; i < gs_round_count; i++) {
        bool found = false;
        for (uint32_t j = 0U; j < gs_seen_count; j++) {
            if (zid_eq(&gs_round[i].zid, &gs_seen[j].zid)) {
                found = true;
                break;
            }
        }
        if (!found) {
            log_node("++ NEW  ", &gs_round[i]);
        }
    }

    /* --- Detect GONE nodes (in seen, not in round) --- */
    for (uint32_t j = 0U; j < gs_seen_count; j++) {
        bool found = false;
        for (uint32_t i = 0U; i < gs_round_count; i++) {
            if (zid_eq(&gs_seen[j].zid, &gs_round[i].zid)) {
                found = true;
                break;
            }
        }
        if (!found) {
            log_node("-- GONE ", &gs_seen[j]);
        }
    }

    DebugP_log("[z_discover] Round %u — %u node(s) visible\r\n",
               (unsigned)gs_round_number, (unsigned)gs_round_count);

    /* --- Update persistent state for next round --- */
    memcpy(gs_seen, gs_round, sizeof(node_info_t) * gs_round_count);
    gs_seen_count = gs_round_count;
}

/* ---- Discovery task ------------------------------------------------------- */
static void z_discover_task(void *arg) {
    (void)arg;

    int net_rc = zenoh_net_init();
    if (net_rc != ZENOH_NET_OK) {
        DebugP_log("[z_discover] Network init failed (%d)\r\n", net_rc);
        vTaskDelete(NULL);
        return;
    }

    char ip_str[16];
    zenoh_net_ip_str(ip_str, sizeof(ip_str));
    DebugP_log("[z_discover] IP: %s  scan interval: %u ms\r\n",
               ip_str, (unsigned)DISCOVER_INTERVAL_MS);

    gs_seen_count  = 0U;
    gs_round_number = 0U;

    while (1) {
        gs_round_count = 0U;
        gs_round_number++;

        DebugP_log("[z_discover] Round %u — scanning...\r\n",
                   (unsigned)gs_round_number);

        z_owned_config_t cfg;
        z_config_default(&cfg);

        z_owned_closure_hello_t closure;
        z_closure(&closure, hello_handler, scout_done, NULL);

        /* z_scout() is synchronous in zenoh-pico: blocks until the scouting
         * timeout elapses, then calls scout_done() before returning.        */
        z_scout(z_move(cfg), z_move(closure), NULL);

        vTaskDelay(pdMS_TO_TICKS(DISCOVER_INTERVAL_MS));
    }

    zenoh_net_deinit();
    vTaskDelete(NULL);
}

/* ---- FreeRTOS entry ------------------------------------------------------- */
void freertos_main(void *arg) {
    (void)arg;
    Drivers_open();
    Board_driversOpen();
    xTaskCreateStatic(z_discover_task, APP_TASK_NAME, APP_TASK_STACK,
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
