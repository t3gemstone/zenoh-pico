/*
 * Copyright (c) 2026 ZettaScale Technology
 * Copyright (c) 2026 T3 Foundation Gemstone Project (www.t3gemstone.org)
 *
 * SPDX-License-Identifier: EPL-2.0 OR Apache-2.0
 *
 * z_queryable — zenoh-pico queryable example for TI AM67A Cortex-R5F.
 *
 * Declares a queryable on "t3/query/data".  Any peer or router that sends
 * a Get request matching this key expression will receive a reply with a
 * fixed value string.  The query handler runs in the context of the zenoh
 * read task.
 *
 * Session mode is selected at compile time via CLIENT_OR_PEER:
 *   0 = client  → connects to a zenoh router at ZENOH_LOCATOR (TCP/UDP unicast)
 *   1 = peer    → UDP multicast, no router needed (DEFAULT)
 *
 * Pair this example with z_get.c to test the request/reply pattern.
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

/* ---- Session mode ---------------------------------------------------------
 * CLIENT_OR_PEER  0 = client (connects to router via TCP/UDP unicast)
 *                 1 = peer   (UDP multicast, no router needed) — DEFAULT
 */
#define CLIENT_OR_PEER  1
#if CLIENT_OR_PEER == 0
#define ZENOH_MODE     "client"
#define ZENOH_LOCATOR  "tcp/192.168.1.100:7447"
#elif CLIENT_OR_PEER == 1
#define ZENOH_MODE     "peer"
#define ZENOH_LOCATOR  "udp/224.0.0.224:7446"
#else
#error "CLIENT_OR_PEER must be 0 (client) or 1 (peer)"
#endif

/* ---- Configuration -------------------------------------------------------- */
#define QUERYABLE_KEYEXPR  "t3/query/data"
#define QUERYABLE_VALUE    "Hello from TI AM67A R5F! (queryable reply)"

/* ---- FreeRTOS task parameters --------------------------------------------- */
#define APP_TASK_NAME  "z_queryable"
#define APP_TASK_PRI   (configMAX_PRIORITIES - 2)
#define APP_TASK_STACK (8192U / sizeof(configSTACK_DEPTH_TYPE))

static StackType_t  gAppTaskStack[APP_TASK_STACK] __attribute__((aligned(32)));
static StaticTask_t gAppTaskObj;

#define MAIN_TASK_STACK (16384U / sizeof(configSTACK_DEPTH_TYPE))
static StackType_t  gMainTaskStack[MAIN_TASK_STACK] __attribute__((aligned(32)));
static StaticTask_t gMainTaskObj;

/* ---- Query handler (called from zenoh read task) -------------------------- */
static void query_handler(z_loaned_query_t *query, void *ctx) {
    (void)ctx;

    z_view_string_t keystr;
    z_keyexpr_as_view_string(z_query_keyexpr(query), &keystr);

    z_view_string_t params;
    z_query_parameters(query, &params);

    DebugP_log("[z_queryable] Received query '%.*s%.*s'\r\n",
               (int)z_string_len(z_loan(keystr)),
               z_string_data(z_loan(keystr)),
               (int)z_string_len(z_loan(params)),
               z_string_data(z_loan(params)));

    /* Print payload if present */
    z_owned_string_t payload_str;
    z_bytes_to_string(z_query_payload(query), &payload_str);
    if (z_string_len(z_loan(payload_str)) > 0U) {
        DebugP_log("[z_queryable]   with payload: '%.*s'\r\n",
                   (int)z_string_len(z_loan(payload_str)),
                   z_string_data(z_loan(payload_str)));
    }
    z_drop(z_move(payload_str));

    /* Reply */
    z_owned_bytes_t reply_payload;
    z_bytes_from_static_str(&reply_payload, QUERYABLE_VALUE);

    z_query_reply_options_t opts;
    z_query_reply_options_default(&opts);
    z_query_reply(query, z_query_keyexpr(query), z_move(reply_payload), &opts);

    DebugP_log("[z_queryable] Replied with '%s'\r\n", QUERYABLE_VALUE);
}

/* ---- Queryable task ------------------------------------------------------- */
static void z_queryable_task(void *arg) {
    (void)arg;

    int net_rc = zenoh_net_init();
    if (net_rc != ZENOH_NET_OK) {
        DebugP_log("[z_queryable] Network init failed (%d)\r\n", net_rc);
        vTaskDelete(NULL);
        return;
    }

    char ip_str[16];
    zenoh_net_ip_str(ip_str, sizeof(ip_str));
    DebugP_log("[z_queryable] IP: %s  mode: %s  keyexpr: %s\r\n",
               ip_str, ZENOH_MODE, QUERYABLE_KEYEXPR);

    z_owned_config_t cfg;
    z_config_default(&cfg);
    zp_config_insert(z_loan_mut(cfg), Z_CONFIG_MODE_KEY, ZENOH_MODE);
#if CLIENT_OR_PEER == 0
    zp_config_insert(z_loan_mut(cfg), Z_CONFIG_CONNECT_KEY, ZENOH_LOCATOR);
#else
    zp_config_insert(z_loan_mut(cfg), Z_CONFIG_LISTEN_KEY, ZENOH_LOCATOR);
#endif

    z_owned_session_t s;
    if (z_open(&s, z_move(cfg), NULL) < 0) {
        DebugP_log("[z_queryable] z_open failed\r\n");
        vTaskDelete(NULL);
        return;
    }

    if (zp_start_read_task(z_loan_mut(s), NULL) < 0 ||
        zp_start_lease_task(z_loan_mut(s), NULL) < 0) {
        DebugP_log("[z_queryable] Failed to start background tasks\r\n");
        z_drop(z_move(s));
        vTaskDelete(NULL);
        return;
    }

    z_view_keyexpr_t ke;
    if (z_view_keyexpr_from_str(&ke, QUERYABLE_KEYEXPR) < 0) {
        DebugP_log("[z_queryable] Invalid key expression\r\n");
        z_drop(z_move(s));
        vTaskDelete(NULL);
        return;
    }

    z_owned_closure_query_t cb;
    z_closure(&cb, query_handler, NULL, NULL);

    z_owned_queryable_t qable;
    if (z_declare_queryable(z_loan(s), &qable, z_loan(ke), z_move(cb), NULL) < 0) {
        DebugP_log("[z_queryable] z_declare_queryable failed\r\n");
        z_drop(z_move(s));
        vTaskDelete(NULL);
        return;
    }

    DebugP_log("[z_queryable] Waiting for queries on '%s'...\r\n",
               QUERYABLE_KEYEXPR);

    while (1) {
        vTaskDelay(pdMS_TO_TICKS(1000U));
    }

    z_drop(z_move(qable));
    zp_stop_read_task(z_loan_mut(s));
    zp_stop_lease_task(z_loan_mut(s));
    z_drop(z_move(s));
    zenoh_net_deinit();
    vTaskDelete(NULL);
}

/* ---- FreeRTOS entry ------------------------------------------------------- */
void freertos_main(void *arg) {
    (void)arg;
    Drivers_open();
    Board_driversOpen();
    xTaskCreateStatic(z_queryable_task, APP_TASK_NAME, APP_TASK_STACK,
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
