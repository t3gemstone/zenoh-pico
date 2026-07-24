/*
 * Copyright (c) 2026 ZettaScale Technology
 * Copyright (c) 2026 T3 Foundation Gemstone Project (www.t3gemstone.org)
 *
 * SPDX-License-Identifier: EPL-2.0 OR Apache-2.0
 *
 * z_sub — zenoh-pico subscriber example for TI AM67A Cortex-R5F.
 *
 * Subscribes to "t3/pub/**" and prints each received sample via DebugP_log.
 *
 * Session mode is selected at compile time via CLIENT_OR_PEER:
 *   0 = client  → connects to a zenoh router at ZENOH_LOCATOR (TCP/UDP unicast)
 *   1 = peer    → UDP multicast, no router needed (DEFAULT)
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
#define SUB_KEYEXPR  "t3/pub/**"

/* ---- FreeRTOS task parameters --------------------------------------------- */
#define APP_TASK_NAME  "z_sub"
#define APP_TASK_PRI   (configMAX_PRIORITIES - 2)
#define APP_TASK_STACK (8192U / sizeof(configSTACK_DEPTH_TYPE))

static StackType_t  gAppTaskStack[APP_TASK_STACK] __attribute__((aligned(32)));
static StaticTask_t gAppTaskObj;

#define MAIN_TASK_STACK (16384U / sizeof(configSTACK_DEPTH_TYPE))
static StackType_t  gMainTaskStack[MAIN_TASK_STACK] __attribute__((aligned(32)));
static StaticTask_t gMainTaskObj;

/* ---- Sample handler (called from zenoh read task) ------------------------- */
static void sample_handler(z_loaned_sample_t *sample, void *ctx) {
    (void)ctx;

    z_view_string_t keystr;
    z_keyexpr_as_view_string(z_sample_keyexpr(sample), &keystr);

    z_owned_string_t value;
    z_bytes_to_string(z_sample_payload(sample), &value);

    DebugP_log("[z_sub] >> ('%.*s': '%.*s')\r\n",
               (int)z_string_len(z_loan(keystr)),
               z_string_data(z_loan(keystr)),
               (int)z_string_len(z_loan(value)),
               z_string_data(z_loan(value)));

    z_drop(z_move(value));
}

/* ---- Subscriber task ------------------------------------------------------ */
static void z_sub_task(void *arg) {
    (void)arg;

    int net_rc = zenoh_net_init();
    if (net_rc != ZENOH_NET_OK) {
        DebugP_log("[z_sub] Network init failed (%d)\r\n", net_rc);
        vTaskDelete(NULL);
        return;
    }

    char ip_str[16];
    zenoh_net_ip_str(ip_str, sizeof(ip_str));
    DebugP_log("[z_sub] IP: %s  mode: %s  keyexpr: %s\r\n",
               ip_str, ZENOH_MODE, SUB_KEYEXPR);

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
        DebugP_log("[z_sub] z_open failed\r\n");
        vTaskDelete(NULL);
        return;
    }

    if (zp_start_read_task(z_loan_mut(s), NULL) < 0 ||
        zp_start_lease_task(z_loan_mut(s), NULL) < 0) {
        DebugP_log("[z_sub] Failed to start background tasks\r\n");
        z_drop(z_move(s));
        vTaskDelete(NULL);
        return;
    }

    z_owned_subscriber_t sub;
    z_owned_closure_sample_t cb;
    z_closure(&cb, sample_handler, NULL, NULL);

    z_view_keyexpr_t ke;
    z_view_keyexpr_from_str(&ke, SUB_KEYEXPR);
    if (z_declare_subscriber(z_loan(s), &sub, z_loan(ke), z_move(cb), NULL) < 0) {
        DebugP_log("[z_sub] z_declare_subscriber failed\r\n");
        z_drop(z_move(s));
        vTaskDelete(NULL);
        return;
    }

    DebugP_log("[z_sub] Waiting for data on '%s'...\r\n", SUB_KEYEXPR);

    while (1) {
        vTaskDelay(pdMS_TO_TICKS(1000U));
    }

    z_drop(z_move(sub));
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
    xTaskCreateStatic(z_sub_task, APP_TASK_NAME, APP_TASK_STACK,
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
