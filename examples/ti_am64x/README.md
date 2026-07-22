# zenoh-pico Examples — TI AM64x (am64x-evm, r5fss0-0, FreeRTOS)

Peer-mode UDP multicast examples for the TI AM64x EVM using:

| Component | Details |
|-----------|---------|
| SoC       | AM6442 (AM64x) |
| Core      | r5fss0-0 (Cortex-R5F, MAIN domain) |
| RTOS      | FreeRTOS |
| Network   | CPSW 3G + lwIP (DHCP) |
| SDK       | TI MCU+ SDK AM64x 12.00.00 |
| Toolchain | TI ARM Clang (tiarmclang) |

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

## Building

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

## Flashing and running

Convert the `.out` file to an appimage using TI's `uart_uniflash.py` or the SBL
boot flow:

```bash
# Using TI's elf2bin (part of MCU+ SDK tools)
${MCU_PLUS_SDK_PATH}/tools/boot/elf2bin/elf2bin.exe \
    --flash-start-address=0x80000000 \
    build/ti_am64x/examples/ti_am64x/z_pub.out \
    z_pub.bin
```

Then flash via UART or SD card according to the am64x-evm Quick Start Guide.

---

## Memory layout

| Region | Address | Size | Usage |
|--------|---------|------|-------|
| R5F_VECS  | `0x00000000` | 64 B | Interrupt vector table |
| R5F_TCMA  | `0x00000040` | 32 KB | (available) |
| R5F_TCMB0 | `0x41010000` | 32 KB | (available) |
| MSRAM     | `0x70080000` | 12 KB | Boot/MPU/cache init code |
| DDR       | `0x80000000` | 14 MB | Application code + data + heap |

---

## Interoperability

Run a zenoh peer on a Linux host connected to the same network:

```bash
# Subscribe to AM64x publisher
zenoh-pico-sub --mode peer --listen udp/224.0.0.224:7446 -k "t3/pub/**"

# Publish to AM64x subscriber
zenoh-pico-pub --mode peer --listen udp/224.0.0.224:7446 -k "t3/pub/data" -v "Hello"
```

Or use the [zenoh router](https://github.com/eclipse-zenoh/zenoh) for client/router mode.
