# zenoh-pico — TI AM67A (J722S) Cortex-R5F Examples

This directory contains three FreeRTOS + lwIP examples that run zenoh-pico on the **main-R5FSS0-0** core of the TI AM67A SoC.

## Requirements

| Tool / Library | Version |
|---|---|
| MCU+ SDK (J722S) | 11.02.01 |
| TI ARM Clang | 3.2.2.LTS |
| TI SysConfig | **≥ 1.26.2** (tested: 1.28.0) |
| CMake | ≥ 3.20 |

Default install paths:
- SDK: `/opt/ti/mcu_plus_sdk_j722s_11_02_01_05/`
- SysConfig: `/opt/ti/sysconfig_1.28.0/`

## Examples

| Target | Description | Key expression |
|---|---|---|
| `z_pub` | Publisher — sends a message every second | `t3/pub/data` |
| `z_sub` | Subscriber — prints received samples via `DebugP_log` | `t3/pub/**` |
| `z_pubsub` | Both combined — two separate FreeRTOS tasks | `t3/pubsub/tx` / `t3/pubsub/rx` |

All examples use **peer mode + UDP multicast** (`udp/224.0.0.224:7446`).

---

## Build Steps

### 1. Generate SysConfig files

If CMake finds `sysconfig_cli.sh` at `/opt/ti/sysconfig_1.28.0/`, it **runs SysConfig automatically during `cmake --build`**. To run manually:

```bash
/opt/ti/sysconfig_1.28.0/sysconfig_cli.sh \
    -s /opt/ti/mcu_plus_sdk_j722s_11_02_01_05/.metadata/product.json \
    -o examples/ti_am67a/syscfg \
    examples/ti_am67a/example.syscfg
```

Generated files land in `syscfg/`. They are excluded by `.gitignore`; CMake regenerates them on a clean build.

> **Note:** If `sysconfig_cli.sh` is in a different location, tell CMake:
> ```bash
> cmake ... -DSYSCONFIG_CLI=/opt/ti/sysconfig_1.28.0/sysconfig_cli.sh
> ```

### 2. CMake configure

```bash
cmake -B build \
    -DCMAKE_TOOLCHAIN_FILE=cmake/toolchain/ti-arm-clang-r5f.cmake \
    -DCGT_TI_ARM_CLANG_PATH=/opt/ti/ti-cgt-armllvm_3.2.2.LTS \
    -DZP_PLATFORM=ti_am67a \
    -DBUILD_EXAMPLES=ON \
    -DCMAKE_BUILD_TYPE=Release
```

> **Note:** `CGT_TI_ARM_CLANG_PATH` can also be set as an environment variable instead of passing `-D`.

### 3. Build

```bash
cmake --build build --target examples -j$(nproc)
```

Output files: `build/examples/ti_am67a/z_pub.out`, `z_sub.out`, `z_pubsub.out`

### 4. Flash to board (J722S EVM)

```bash
# Convert to RPRC
${MCU_PLUS_SDK_PATH}/tools/boot/out2rprc/out2rprc.exe z_pub.out z_pub.rprc

# Create multi-core image
${MCU_PLUS_SDK_PATH}/tools/boot/multicoreImageGen/MulticoreImageGen \
    LE 55 z_pub.appimage \
    0 ${MCU_PLUS_SDK_PATH}/source/drivers/sciclient/sysfw/binaries/sysfw.bin \
    4 z_pub.rprc

# Flash via UART (uniflash or tiboot3 bootloader)
python3 ${MCU_PLUS_SDK_PATH}/tools/boot/uart_uniflash.py \
    --cfg tools/boot/sbl_prebuilt/j722s-evm/default_sbl_uart_hs_fs.cfg \
    --appimage z_pub.appimage
```

---

## Architecture Notes

### FreeRTOS task flow

```
main()
  └── xTaskCreateStatic(freertos_main)
        └── vTaskStartScheduler()
              └── freertos_main():
                    Drivers_open()
                    Board_driversOpen()
                    zenoh_net_init()   ← CPSW + lwIP + DHCP
                    z_open(session)
                    zp_start_read_task / zp_start_lease_task
                    └── application task(s)
```

### Network init (`common/zenoh_net_init.c`)

`zenoh_net_init()`:

1. Calls `tcpip_init(_tcpip_init_done_cb)` — starts the lwIP tcpip_thread
2. Inside the callback (runs in tcpip_thread context):
   - `LwipifEnetApp_getHandle()` — obtain CPSW Enet driver handle
   - `LwipifEnetApp_netifOpen()` — open lwIP netif
   - `dhcp_start()` — start DHCP
   - `LwipifEnetApp_startSchedule()` — start Enet Rx/Tx polling task
3. Calling task waits for DHCP IP assignment on a binary semaphore (timeout: 15 s)

**To use a static IP**, define before including `zenoh_net_init.h`:
```c
#define ZENOH_NET_STATIC_IP   "192.168.1.100"
#define ZENOH_NET_STATIC_MASK "255.255.255.0"
#define ZENOH_NET_STATIC_GW   "192.168.1.1"
```

### Memory map (linker file)

| Region | Address | Size | Usage |
|---|---|---|---|
| R5F_VECS | 0x00000000 | 64 B | Exception vectors |
| R5F_TCMA | 0x00000040 | 32 KB | TCM-A (fast code) |
| R5F_TCMB0 | 0x41010000 | 16 KB | TCM-B0 (fast data) |
| DDR_CODE_DATA | 0xA2200000 | 14 MB | Code + data + stack + heap |

Stack size: 16 KB | Heap size: 64 KB (lwIP pbuf pool)

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `SysConfig CLI not found` | `sysconfig_cli.sh` not on search path | Pass `-DSYSCONFIG_CLI=...` |
| `SysConfig version mismatch` | SysConfig < 1.26.2 installed | Install SysConfig 1.28.0 |
| `LwipifEnetApp_netifOpen failed` | Enet driver did not start | Check `Drivers_open()` / `Board_driversOpen()` |
| DHCP timeout | Cable/switch issue | Check physical link; try static IP |
| `z_open failed` | Multicast unreachable | Verify router/switch allows `224.0.0.224` multicast |
| Linker error: `*.cmd` not found | `.gitignore` blocking | Extension must be `.cmd.ld`, not `.cmd` |
