/*
 * Copyright (c) 2026 ZettaScale Technology
 * Copyright (c) 2026 T3 Foundation Gemstone Project (www.t3gemstone.org)
 *
 * SPDX-License-Identifier: EPL-2.0 OR Apache-2.0
 *
 * z_node — zenoh-pico node presence + status example for TI AM67A Cortex-R5F.
 *
 * Implements a three-layer presence pattern so any node can identify itself
 * and remain discoverable regardless of when peers join the network:
 *
 *   1. Announcement put  — published once on startup so already-listening
 *                          peers receive the info immediately.
 *
 *   2. Info queryable    — declared on "t3/nodes/<NODE_NAME>/info" so any
 *                          peer that joins AFTER this node can still retrieve
 *                          the static metadata with a z_get() at any time.
 *
 *   3. Status publisher  — periodic heartbeat on "t3/nodes/<NODE_NAME>/status"
 *                          carrying dynamic data (uptime, free heap, sequence).
 *
 * Payload format — zenoh-pico native serialization (ze_serializer_t):
 *   Little-endian binary, field-by-field, no JSON overhead.
 *   Encoding tag: "zenoh/serialized" (Z_FEATURE_ENCODING_VALUES=1) or
 *                 default (Z_FEATURE_ENCODING_VALUES=0).
 *
 *   Info payload  (5 fields, written in order):
 *     [str]  name       — NODE_NAME
 *     [str]  ip         — dotted-decimal IPv4
 *     [str]  fw         — NODE_FW_VER
 *     [buf]  zid        — 16-byte zenoh session ID
 *     [str]  mode       — "peer" or "client"
 *
 *   Status payload (3 fields, written in order):
 *     [u32]  seq        — monotonically increasing publication counter
 *     [u32]  uptime_s   — seconds since scheduler start
 *     [u32]  heap_free  — free FreeRTOS heap bytes at publish time
 *
 *   String fields are length-prefixed (varint) then UTF-8 bytes.
 *   u32 fields are 4 bytes, little-endian.
 *
 * Sample log output:
 *   [z_node] IP: 192.168.1.50  name: am67a-r5f-01  mode: peer
 *   [z_node] Info  : t3/nodes/am67a-r5f-01/info
 *   [z_node] Status: t3/nodes/am67a-r5f-01/status  (every 5000 ms)
 *   [z_node] ZID: A3B2C1D0...0000
 *   [z_node] Announced presence
 *   [z_node] Query on t3/nodes/am67a-r5f-01/info — replied
 *   [z_node] Status #1: uptime=5s heap_free=49152
 *
 * Pair with z_discover.c which scouts for peers and queries their /info.
 *
 * Session mode is selected at compile time via CLIENT_OR_PEER:
 *   0 = client  → connects to a zenoh router at ZENOH_LOCATOR (TCP/UDP unicast)
 *   1 = peer    → UDP multicast, no router needed (DEFAULT)
 */

#include <stdint.h>
#include <stdbool.h>

#include "ti_drivers_config.h"
#include "ti_drivers_open_close.h"
#include "ti_board_config.h"
#include "ti_board_open_close.h"

#include <kernel/dpl/DebugP.h>

#include "FreeRTOS.h"
#include "task.h"

#include <string.h>

#include <zenoh-pico.h>
#include "zenoh_net_init.h"

#include "example.h"

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

/* ---- Node identity --------------------------------------------------------
 * Override at cmake time: add_ti_am67a_example(z_node z_node.c)
 * and pass: target_compile_definitions(z_node PRIVATE NODE_NAME="am67a-r5f-02")
 */
#ifndef NODE_NAME
#define NODE_NAME    "am67a-r5f-01"
#endif
#ifndef NODE_FW_VER
#define NODE_FW_VER  "1.0.0"
#endif

/* ---- Key expressions ------------------------------------------------------ */
#define KE_INFO    "t3/nodes/" NODE_NAME "/info"
#define KE_STATUS  "t3/nodes/" NODE_NAME "/status"

/* ---- Timing --------------------------------------------------------------- */
#define STATUS_INTERVAL_MS  5000U

/* ---- FreeRTOS task parameters --------------------------------------------- */
#define APP_TASK_NAME  "z_node"
#define APP_TASK_PRI   (configMAX_PRIORITIES - 2)
#define APP_TASK_STACK (8192U / sizeof(configSTACK_DEPTH_TYPE))

static StackType_t  gAppTaskStack[APP_TASK_STACK] __attribute__((aligned(32)));
static StaticTask_t gAppTaskObj;

#define MAIN_TASK_STACK (16384U / sizeof(configSTACK_DEPTH_TYPE))
static StackType_t  gMainTaskStack[MAIN_TASK_STACK] __attribute__((aligned(32)));
static StaticTask_t gMainTaskObj;

/* ---- Queryable context (set once at startup, read by query handler) ------- */
static char   gs_ip_str[16];
static z_id_t gs_own_zid;

/* Populate a node_info_t from the current node globals. */
static node_info_t make_node_info(void)
{
    node_info_t info;
    info.name = NODE_NAME;
    info.ip   = gs_ip_str;
    info.fw   = NODE_FW_VER;
    memcpy(&info.zid, gs_own_zid.id, sizeof(gs_own_zid.id));
    info.mode = ZENOH_MODE;
    return info;
}

/* Populate a z_put_options_t / z_publisher_put_options_t with the encoding. */
#if Z_FEATURE_ENCODING_VALUES == 1
static void set_serialized_encoding(z_put_options_t *opts) {
    z_put_options_default(opts);
    z_owned_encoding_t enc;
    z_encoding_clone(&enc, z_encoding_zenoh_serialized());
    opts->encoding = z_move(enc);
}
static void set_serialized_encoding_pub(z_publisher_put_options_t *opts) {
    z_publisher_put_options_default(opts);
    z_owned_encoding_t enc;
    z_encoding_clone(&enc, z_encoding_zenoh_serialized());
    opts->encoding = z_move(enc);
}
#endif

/* ---- ZID log helper ------------------------------------------------------- */
static void log_zid(const z_id_t *zid) {
    /* Print all 16 bytes as hex pairs (32 hex chars) */
    char buf[33] = {0};
    for (int i = 0; i < 16; i++) {
        static const char hex[] = "0123456789ABCDEF";
        buf[i * 2]     = hex[(zid->id[i] >> 4) & 0xFU];
        buf[i * 2 + 1] = hex[ zid->id[i]       & 0xFU];
    }
    DebugP_log("[z_node] ZID: %s\r\n", buf);
}

/* ---- Queryable handler (called from zenoh read task) ---------------------- */
static void info_query_handler(z_loaned_query_t *query, void *ctx) {
    (void)ctx;

    z_view_string_t keystr;
    z_keyexpr_as_view_string(z_query_keyexpr(query), &keystr);
    DebugP_log("[z_node] Query on %.*s — replied\r\n",
               (int)z_string_len(z_loan(keystr)),
               z_string_data(z_loan(keystr)));

    node_info_t info = make_node_info();
    z_owned_bytes_t payload;
    if (node_info_t_to_bytes(&info, &payload) != Z_OK) {
        DebugP_log("[z_node] Serialization failed in query handler\r\n");
        return;
    }

    z_query_reply_options_t opts;
    z_query_reply_options_default(&opts);

#if Z_FEATURE_ENCODING_VALUES == 1
    z_owned_encoding_t enc;
    z_encoding_clone(&enc, z_encoding_zenoh_serialized());
    opts.encoding = z_move(enc);
#endif

    z_query_reply(query, z_query_keyexpr(query), z_move(payload), &opts);
}

/* ---- Node task ------------------------------------------------------------ */
static void z_node_task(void *arg) {
    (void)arg;

    /* 1. Network */
    int net_rc = zenoh_net_init();
    if (net_rc != ZENOH_NET_OK) {
        DebugP_log("[z_node] Network init failed (%d)\r\n", net_rc);
        vTaskDelete(NULL);
        return;
    }

    zenoh_net_ip_str(gs_ip_str, sizeof(gs_ip_str));
    DebugP_log("[z_node] IP: %s  name: %s  mode: %s\r\n",
               gs_ip_str, NODE_NAME, ZENOH_MODE);
    DebugP_log("[z_node] Info  : %s\r\n", KE_INFO);
    DebugP_log("[z_node] Status: %s  (every %u ms)\r\n",
               KE_STATUS, (unsigned)STATUS_INTERVAL_MS);

    /* 2. Open session */
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
        DebugP_log("[z_node] z_open failed\r\n");
        vTaskDelete(NULL);
        return;
    }

    if (zp_start_read_task(z_loan_mut(s), NULL) < 0 ||
        zp_start_lease_task(z_loan_mut(s), NULL) < 0) {
        DebugP_log("[z_node] Failed to start background tasks\r\n");
        z_drop(z_move(s));
        vTaskDelete(NULL);
        return;
    }

    /* 3. Capture own ZID (used by both announce and queryable) */
    gs_own_zid = z_info_zid(z_loan(s));
    log_zid(&gs_own_zid);

    /* 4. Announce presence: one-shot put → already-listening peers receive info */
    {
        z_view_keyexpr_t ke;
        z_view_keyexpr_from_str(&ke, KE_INFO);

        node_info_t info = make_node_info();
        z_owned_bytes_t payload;
        if (node_info_t_to_bytes(&info, &payload) != Z_OK) {
            DebugP_log("[z_node] Info serialization failed\r\n");
            z_drop(z_move(s));
            vTaskDelete(NULL);
            return;
        }

#if Z_FEATURE_ENCODING_VALUES == 1
        z_put_options_t put_opts;
        set_serialized_encoding(&put_opts);
        z_put(z_loan(s), z_loan(ke), z_move(payload), &put_opts);
#else
        z_put(z_loan(s), z_loan(ke), z_move(payload), NULL);
#endif
        DebugP_log("[z_node] Announced presence\r\n");
    }

    /* 5. Declare queryable so late-joining peers can retrieve info on demand */
    z_owned_closure_query_t qcb;
    z_closure(&qcb, info_query_handler, NULL, NULL);

    z_view_keyexpr_t qke;
    z_view_keyexpr_from_str(&qke, KE_INFO);

    z_owned_queryable_t qable;
    if (z_declare_queryable(z_loan(s), &qable, z_loan(qke), z_move(qcb), NULL) < 0) {
        DebugP_log("[z_node] z_declare_queryable failed\r\n");
        z_drop(z_move(s));
        vTaskDelete(NULL);
        return;
    }

    /* 6. Declare persistent publisher for status */
    z_view_keyexpr_t ske;
    z_view_keyexpr_from_str(&ske, KE_STATUS);

    z_owned_publisher_t status_pub;
    if (z_declare_publisher(z_loan(s), &status_pub, z_loan(ske), NULL) < 0) {
        DebugP_log("[z_node] z_declare_publisher(status) failed\r\n");
        z_drop(z_move(qable));
        z_drop(z_move(s));
        vTaskDelete(NULL);
        return;
    }

    /* 7. Periodic status loop */
    for (uint32_t seq = 1U; ; seq++) {
        vTaskDelay(pdMS_TO_TICKS(STATUS_INTERVAL_MS));

        uint32_t uptime_s  = (uint32_t)(xTaskGetTickCount() / configTICK_RATE_HZ);
        uint32_t heap_free = (uint32_t)xPortGetFreeHeapSize();

        node_status_t status = { .seq = seq, .uptime_s = uptime_s, .heap_free = heap_free };
        z_owned_bytes_t payload;
        if (node_status_t_to_bytes(&status, &payload) != Z_OK) {
            DebugP_log("[z_node] Status serialization failed\r\n");
            continue;
        }

#if Z_FEATURE_ENCODING_VALUES == 1
        z_publisher_put_options_t pub_opts;
        set_serialized_encoding_pub(&pub_opts);
        z_publisher_put(z_loan(status_pub), z_move(payload), &pub_opts);
#else
        z_publisher_put(z_loan(status_pub), z_move(payload), NULL);
#endif

        DebugP_log("[z_node] Status #%u: uptime=%us heap_free=%u\r\n",
                   (unsigned)seq, (unsigned)uptime_s, (unsigned)heap_free);
    }

    /* Unreachable */
    z_drop(z_move(status_pub));
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
    xTaskCreateStatic(z_node_task, APP_TASK_NAME, APP_TASK_STACK,
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
