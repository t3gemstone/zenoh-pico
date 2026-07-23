# zenoh-pico Examples — TI AM64x (am64x-evm, r5fss0-0, FreeRTOS)

Peer-mode UDP multicast examples for the TI AM64x EVM using:

| Component | Details |
|-----------|---------|
| SoC       | AM6442 (AM64x) |
| Core      | r5fss0-0 (Cortex-R5F, MAIN domain) |
| RTOS      | FreeRTOS |
| SDK       | TI MCU+ SDK AM64x 12.00.00 |
| Toolchain | TI ARM Clang (tiarmclang) |

The examples support two transport backends selectable at build time:

| Mode | CMake flag | Hardware needed | Linux side |
|---|---|---|---|
| **CPSW Ethernet** (default) | *(none)* | CPSW PHY + cable | standard IP routing / DHCP |
| **IPC / RPMsg** | `-DZENOH_TI_AM64X_IPC=ON` | none (shared DRAM) | `rpmsg_net.ko` kernel module |

---

## Prerequisites

| Tool | Version | Default location |
|------|---------|-----------------|
| TI MCU+ SDK AM64x | 12.00.00 | `/opt/ti/mcu_plus_sdk_am64x_12_00_00_27/` |
| TI ARM Clang CGT  | 3.2.2.LTS | `/opt/ti/ti-cgt-armllvm_3.2.2.LTS/` |
| TI SysConfig      | 1.21.2 – 1.28.0 | `/opt/ti/sysconfig_1.28.0/` |
| CMake             | ≥ 3.20 | — |
| Python 3          | ≥ 3.8  | — |

Install the SDK from [TI E2E / MySecureSoftware](https://www.ti.com/tool/download/MCU-PLUS-SDK-AM64X)
and the CGT from [TI ARM Clang](https://www.ti.com/tool/ARM-CGT-CLANG).

---

## Building — CPSW Ethernet mode (default)

From the zenoh-pico repository root:

```bash
cmake \
  -DCMAKE_TOOLCHAIN_FILE=cmake/toolchain/ti-arm-clang-r5f.cmake \
  -DZP_PLATFORM=ti_am64x \
  -DBUILD_SHARED_LIBS=OFF \
  -DBUILD_EXAMPLES=ON \
  -B build/ti_am64x -S .

cmake --build build/ti_am64x --target examples -j$(nproc)
```

Outputs: `build/ti_am64x/examples/ti_am64x/z_pub.out`, `z_sub.out`, `z_pubsub.out`

### Toolchain auto-detection

`cmake/toolchain/ti-arm-clang-r5f.cmake` automatically finds tiarmclang under
`/opt/ti/ti-cgt-armllvm_*`. Override with:

```bash
cmake ... -DCGT_TI_ARM_CLANG_PATH=/path/to/ti-cgt-armllvm_3.2.2.LTS ...
```

### SDK auto-detection

`cmake/platforms/ti_am64x.cmake` looks for the SDK at
`/opt/ti/mcu_plus_sdk_am64x_12_00_00_27`. Override with:

```bash
cmake ... -DMCU_PLUS_SDK_PATH=/path/to/mcu_plus_sdk_am64x_12_00_00_27 ...
```

---

## SysConfig code generation

CMake runs SysConfig automatically if `sysconfig_cli.sh` is found. The generated
files go into `examples/ti_am64x/syscfg/` (gitignored).

To run manually:

```bash
/opt/ti/sysconfig_1.28.0/sysconfig_cli.sh \
  -s /opt/ti/mcu_plus_sdk_am64x_12_00_00_27/.metadata/product.json \
  -o examples/ti_am64x/syscfg \
  examples/ti_am64x/example.syscfg

python3 examples/ti_am64x/patch_ti_enet_init.py \
  examples/ti_am64x/syscfg/ti_enet_init.c
```

The `patch_ti_enet_init.py` script fixes a SysConfig code-generation bug where
a `const` struct is used as a static initializer (invalid C).

---

## Examples

| Binary | Description |
|--------|-------------|
| `z_pub.out` | Publishes `"[N] Hello from TI AM64x R5F!"` on `t3/pub/data` every second |
| `z_sub.out` | Subscribes to `t3/pub/**` and prints each message |
| `z_pubsub.out` | Combined: publishes on `t3/pubsub/tx`, subscribes to `t3/pubsub/rx` |

All examples use **peer mode** over **UDP multicast** (`udp/224.0.0.224:7446`).

---

## Flashing and running — CPSW mode

Convert the `.out` file to an appimage using TI's `uart_uniflash.py` or the SBL
boot flow:

```bash
# Using TI's out2rprc + MulticoreImageGen (part of MCU+ SDK tools)
${MCU_PLUS_SDK_PATH}/tools/boot/out2rprc/out2rprc.exe \
    build/ti_am64x/examples/ti_am64x/z_pub.out z_pub.rprc

${MCU_PLUS_SDK_PATH}/tools/boot/multicoreImageGen/MulticoreImageGen \
    LE 55 z_pub.appimage \
    0 ${MCU_PLUS_SDK_PATH}/source/drivers/sciclient/sysfw/binaries/sysfw.bin \
    4 z_pub.rprc
```

Then flash via UART or SD card according to the am64x-evm Quick Start Guide.

---

## IPC / RPMsg Transport Mode

When CPSW Ethernet is unavailable or undesirable (e.g. the Ethernet PHY is used
by another core, or the board has no network cable), zenoh-pico can tunnel all
traffic over the TI IPC RPMessage channel between the R5F and the Linux A53 core.
From zenoh-pico's perspective the transport is unchanged — it still uses lwIP +
UDP; the difference is that Ethernet frames travel through shared DRAM VRINGs
instead of over a PHY.

```
R5F (FreeRTOS)                          Linux A53
──────────────                          ─────────
zenoh-pico                              zenoh router / peer
   │ UDP/IP                                  │ UDP/IP
lwIP netif (rp0)                        rpmsg0 (virtual Ethernet)
   │ Ethernet frames                         │
rpmsg_lwip_netif.c                      rpmsg_net.ko
   │ RPMessage_send/recv                     │ rpmsg_endpoint
   └──────── VRING shared DRAM ──────────────┘
              (0xA0000000, non-cached)
```

**IP addressing** (static, compile-time defaults):

| Side | Interface | Address |
|---|---|---|
| R5F | lwIP `rp0` | `192.168.200.1/30` |
| Linux | `rpmsg0` | `192.168.200.2/30` |

Override the R5F address at compile time:

```cmake
target_compile_definitions(z_pub PRIVATE
    RPMSG_NETIF_IP="192.168.100.1"
    RPMSG_NETIF_MASK="255.255.255.252"
    RPMSG_NETIF_GW="192.168.100.2")
```

**MTU**: The default VirtIO VRING buffer is 512 bytes -> RPMsg payload max = 496 bytes -> IP MTU = **478 bytes** (496 − 14 ETH_HLEN − 4 ETH_FCS). zenoh-pico's `Z_FEATURE_FRAGMENTATION=1` (already enabled) handles messages larger than the MTU transparently.

### Build — IPC mode

#### 1. Generate SysConfig files (IPC variant)

CMake runs SysConfig automatically when `-DZENOH_TI_AM64X_IPC=ON` is set. To run manually:

```bash
/opt/ti/sysconfig_1.28.0/sysconfig_cli.sh \
    -s /opt/ti/mcu_plus_sdk_am64x_12_00_00_27/.metadata/product.json \
    -o examples/ti_am64x/syscfg_ipc \
    examples/ti_am64x/example_ipc.syscfg

python3 examples/ti_am64x/patch_ti_ipc_config.py \
    examples/ti_am64x/syscfg_ipc/ti_drivers_config.c
```

The `patch_ti_ipc_config.py` script fixes a SysConfig code-generation issue:
without `memory_configurator`, the generated `ti_drivers_config.c` has empty
subscripts (`gIpcSharedMem[]`) for inter-R5F VRING addresses. The patch replaces
those with `0U` — safe since this firmware only uses Linux IPC, not R5F-to-R5F
VRING communication.

Generated files land in `syscfg_ipc/`. They are excluded by `.gitignore`; CMake
regenerates and patches them automatically on a clean build.

#### 2. CMake configure — IPC mode

```bash
cmake -B build_ipc \
    -DCMAKE_TOOLCHAIN_FILE=cmake/toolchain/ti-arm-clang-r5f.cmake \
    -DCGT_TI_ARM_CLANG_PATH=/opt/ti/ti-cgt-armllvm_3.2.2.LTS \
    -DZP_PLATFORM=ti_am64x \
    -DZENOH_TI_AM64X_IPC=ON \
    -DBUILD_EXAMPLES=ON \
    -DCMAKE_BUILD_TYPE=Release
```

#### 3. Build

```bash
cmake --build build_ipc --target examples -j$(nproc)
```

Output files: `build_ipc/examples/ti_am64x/z_pub.out`, `z_sub.out`, `z_pubsub.out`

#### 4. Flash to board (remoteproc)

In IPC mode the R5F firmware is loaded by Linux remoteproc (not SBL). Copy the
`.out` ELF directly to the remoteproc firmware directory:

```bash
# On the target Linux system:
sudo cp z_pub.out /lib/firmware/am64x-mcu-r5f0_0-fw
echo start | sudo tee /sys/class/remoteproc/remoteproc0/state
```

The ELF must be placed at `0xA0100000` (DDR_0 resource table) and
`0xA0101000` (DDR_1 code/data) as defined in `linker/r5fss0-0_ipc.cmd.ld`.
These addresses must be covered by a `reserved-memory` carveout in the Linux
device tree.

#### 5. Linux side — load rpmsg_net kernel module

The `rpmsg_net` kernel module creates a virtual Ethernet device `rpmsg0` backed
by the RPMsg channel. Source: [https://github.com/t3gemstone/rpmsg-net](https://github.com/t3gemstone/rpmsg-net)

```bash
# Clone and build the kernel module (once, on the target Linux system)
git clone https://github.com/t3gemstone/rpmsg-net
cd rpmsg-net
make -C /lib/modules/$(uname -r)/build M=$(pwd) modules

# Load and configure (after R5F firmware has started)
sudo insmod rpmsg_net.ko
sudo ip addr add 192.168.200.2/30 dev rpmsg0
sudo ip link set rpmsg0 up

# Add multicast route if using peer mode
sudo ip route add 224.0.0.0/4 dev rpmsg0
```

When the R5F firmware boots it announces the `"rpmsg-enet"` service; `rpmsg_net.ko`
matches on that name and the `rpmsg0` device becomes active.

#### 6. Run zenoh on Linux

```bash
# Client mode: R5F connects to a router on Linux
zenohd -l udp/192.168.200.2:7447

# Peer mode: multicast discovery (no router needed)
# (no extra steps — R5F uses udp/224.0.0.224:7446 by default)
```

### Device tree / VRING carveout

The VRING shared memory at `0xA0000000` (1 MB) must match the `reserved-memory`
carveout in the Linux device tree for the AM64x r5fss0-0 remoteproc. If your DTS
uses a different address, update `CONFIG_MPU_LINUX_IPC.baseAddr` in
`example_ipc.syscfg` and the `LINUX_IPC_SHM_MEM` region in
`linker/r5fss0-0_ipc.cmd.ld` accordingly.

---

## Architecture Notes

### FreeRTOS task flow

**CPSW Ethernet mode (default):**

```
main()
  └── xTaskCreateStatic(freertos_main)
        └── vTaskStartScheduler()
              └── freertos_main():
                    Drivers_open()
                    Board_driversOpen()
                    zenoh_net_init()   <- CPSW + lwIP + DHCP
                    z_open(session)
                    zp_start_read_task / zp_start_lease_task
                    └── application task(s)
```

**IPC / RPMsg mode (`ZENOH_TI_AM64X_IPC=ON`):**

```
main()
  └── xTaskCreateStatic(freertos_main)
        └── vTaskStartScheduler()
              └── freertos_main():
                    Drivers_open()
                    Board_driversOpen()
                    zenoh_net_init()   <- RPMsg netif + static IP
                      ├── tcpip_init -> rpmsg_lwip_netif_init()
                      │     ├── RPMessage_waitForLinuxReady()
                      │     ├── RPMessage_construct()
                      │     ├── RPMessage_announce("rpmsg-enet")
                      │     └── xTaskCreateStatic(rpmsg_rx)  <- recv loop
                      └── rpmsg_lwip_netif_wait_peer()  <- waits for first frame
                    z_open(session)
                    zp_start_read_task / zp_start_lease_task
                    └── application task(s)
```

### Network init — IPC mode (`common/zenoh_net_init_rpmsg.c`)

`zenoh_net_init()` in IPC mode:

1. Calls `tcpip_init(_tcpip_init_done_cb)` — starts the lwIP tcpip_thread
2. Inside the callback:
   - `netif_add()` with `rpmsg_lwip_netif_init` — registers the RPMsg netif with static IP `192.168.200.1/30`
   - `netif_set_default()` / `netif_set_up()` / `netif_set_link_up()`
3. Calling task waits on the init semaphore
4. `rpmsg_lwip_netif_wait_peer()` — blocks until Linux sends the first frame (timeout: 15 s)

Inside `rpmsg_lwip_netif_init()`:

1. `RPMessage_waitForLinuxReady()` — waits for Linux remoteproc to finish booting (timeout: 10 s)
2. `RPMessage_construct()` — opens local endpoint `RPMSG_NETIF_ENDPT` (default: 14)
3. `RPMessage_announce(CSL_CORE_ID_A53SS0_0, RPMSG_NETIF_ENDPT, "rpmsg-enet")` — advertises the service; `rpmsg_net.ko` matches on `"rpmsg-enet"` and probes the device
4. Spawns `rpmsg_rx` FreeRTOS task (calls `RPMessage_recv` in a loop; injects frames into lwIP via `netif->input()`)
5. Sets `netif->mtu = 478`, `netif->output = etharp_output`, `netif->linkoutput = _netif_linkoutput`

### Memory map

**CPSW Ethernet mode (linker/r5fss0-0.cmd.ld):**

| Region | Address | Size | Usage |
|--------|---------|------|-------|
| R5F_VECS  | `0x00000000` | 64 B | Interrupt vector table |
| R5F_TCMA  | `0x00000040` | 32 KB | (available) |
| R5F_TCMB0 | `0x41010000` | 32 KB | (available) |
| MSRAM     | `0x70080000` | 12 KB | Boot/MPU/cache init code |
| DDR       | `0x80000000` | 14 MB | Application code + data + heap |

**IPC mode (linker/r5fss0-0_ipc.cmd.ld):**

| Region | Address | Size | Usage |
|--------|---------|------|-------|
| R5F_VECS  | `0x00000000` | 64 B | Interrupt vector table |
| R5F_TCMA  | `0x00000040` | 32 KB | (available) |
| R5F_TCMB0 | `0x41010000` | 32 KB | (available) |
| MSRAM     | `0x70080000` | 256 KB | (available) |
| DDR_0     | `0xA0100000` | 4 KB | `.resource_table` (remoteproc reads this) |
| DDR_1     | `0xA0101000` | ~15 MB | Code + data + heap + stacks |
| LINUX_IPC | `0xA0000000` | 1 MB | Linux VRING carveout (non-cached) |
| USER_SHM  | `0xA5000000` | 128 B | `.bss.user_shared_mem` |
| LOG_SHM   | `0xA5000080` | ~16 KB | `.bss.log_shared_mem` |
| RTOS_IPC  | `0xA5004000` | 48 KB | `.bss.ipc_vring_mem` |

Stack size: 16 KB | Heap size: 64 KB (lwIP pbuf pool)

---

## Interoperability

Run a zenoh peer on a Linux host connected to the same network:

```bash
# Subscribe to AM64x publisher (CPSW mode)
zenoh-pico-sub --mode peer --listen udp/224.0.0.224:7446 -k "t3/pub/**"

# Publish to AM64x subscriber (CPSW mode)
zenoh-pico-pub --mode peer --listen udp/224.0.0.224:7446 -k "t3/pub/data" -v "Hello"
```

Or use the [zenoh router](https://github.com/eclipse-zenoh/zenoh) for client/router mode.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `SysConfig CLI not found` | `sysconfig_cli.sh` not on search path | Pass `-DSYSCONFIG_CLI=...` |
| `SysConfig version mismatch` | SysConfig < 1.21.2 installed | Install SysConfig 1.28.0 |
| `LwipifEnetApp_netifOpen failed` | Enet driver did not start | Check `Drivers_open()` / `Board_driversOpen()` |
| DHCP timeout | Cable/switch issue | Check physical link; try static IP |
| `z_open failed` | Multicast unreachable | Verify router/switch allows `224.0.0.224` multicast |
| Linker error: `*.cmd` not found | `.gitignore` blocking | Extension must be `.cmd.ld`, not `.cmd` |

**IPC / RPMsg mode specific:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `RPMessage_waitForLinuxReady timed out` | Linux remoteproc not started or DT mismatch | Verify `echo start > /sys/class/remoteproc/remoteproc0/state`; check VRING carveout in DTS vs `example_ipc.syscfg` |
| `rpmsg_net.ko` not found after `insmod` | Service name mismatch | R5F announces `"rpmsg-enet"`; check `rpmsg_net.c` service name matches |
| `rpmsg0` device does not appear | Module not loaded, or R5F not booted yet | Boot R5F first, then `insmod rpmsg_net.ko`; check `dmesg` for probe messages |
| Peer not ready after 15 s | Linux never sent first frame | Check `ip link set rpmsg0 up`; verify `ip addr` on `rpmsg0`; try `ping 192.168.200.1` from Linux |
| Large zenoh messages dropped | MTU mismatch | Confirm `rpmsg_net.ko` was built with 512-byte VRING size (478-byte MTU) |
| Linker: undefined `enet_cpsw` symbols in IPC mode | Old build directory used | Reconfigure with `-DZENOH_TI_AM64X_IPC=ON` in a fresh build dir |
| Build error: `expected expression` in `ti_drivers_config.c` | `patch_ti_ipc_config.py` not run | Run patch manually: `python3 patch_ti_ipc_config.py syscfg_ipc/ti_drivers_config.c` |
