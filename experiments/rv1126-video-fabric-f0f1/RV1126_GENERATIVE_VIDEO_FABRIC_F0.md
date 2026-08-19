# RV1126 GENERATIVE VIDEO FABRIC — F0/F1

Status: SOFTWARE WORKLOAD / CONTRACT SIMULATION. Not RV1126 timing proof. QEMU ARM `virt` is not used here and would not be RV1126 silicon emulation.

## F0 result at the declared median point

Median point: 40% effective NPU utilization, 125 MiB/s full-duplex controller link, local 12 Mbit/s encoded output, 250 ms offline playback buffer.

| Input | RV nodes | fps achieved | misses | P95 ms | transfer MiB/s | compute headroom | classification |
|---|---:|---:|---:|---:|---:|---:|---|
| 360p5 | 4 | 59.79 | 0 | 124.2 | 9.3 | 1.87x | HEADROOM |
| 360p5 | 8 | 60.38 | 0 | 86.1 | 17.1 | 3.74x | HEADROOM |
| 360p5 | 16 | 60.63 | 0 | 73.8 | 24.8 | 7.49x | HEADROOM |
| 360p10 | 4 | 59.86 | 0 | 79.7 | 17.1 | 1.98x | HEADROOM |
| 360p10 | 8 | 60.10 | 0 | 54.5 | 25.0 | 3.95x | HEADROOM |
| 360p10 | 16 | 60.16 | 0 | 54.5 | 25.1 | 7.90x | HEADROOM |
| 360p15 | 4 | 59.90 | 0 | 48.0 | 24.9 | 2.09x | HEADROOM |
| 360p15 | 8 | 60.03 | 0 | 48.0 | 25.0 | 4.18x | HEADROOM |
| 360p15 | 16 | 60.03 | 0 | 48.0 | 25.0 | 8.37x | HEADROOM |

All nine median encoded cases meet the 250 ms offline-buffer deadline in this synthetic model. This is a scheduler/capacity result only; the 30 GOP reconstructed-frame budget is ASSUMED, not measured RV1126 model timing.

## Bandwidth crossover at 40% utilization

Minimum tested full-duplex bandwidth with zero F0 deadline misses:

| Input | nodes | local encoded output | raw 1080p gather |
|---|---:|---:|---:|
| 360p5 | 4 | 50 MiB/s | 200 MiB/s |
| 360p5 | 8 | 50 MiB/s | 200 MiB/s |
| 360p5 | 16 | 50 MiB/s | 400 MiB/s |
| 360p10 | 4 | 12.5 MiB/s | 200 MiB/s |
| 360p10 | 8 | 12.5 MiB/s | 200 MiB/s |
| 360p10 | 16 | 12.5 MiB/s | 200 MiB/s |
| 360p15 | 4 | 25 MiB/s | 200 MiB/s |
| 360p15 | 8 | 25 MiB/s | 200 MiB/s |
| 360p15 | 16 | 25 MiB/s | 200 MiB/s |

Important F0 discovery: lower authoritative fps can be harder on burst latency even when its average input rate is lower. At 5 fps, eleven reconstructed display positions become eligible together when the next authoritative frame arrives. With only a 250 ms playback buffer, this raises the tested encoded-link crossover to 50 MiB/s. At 10 fps the tested grid passes at 12.5 MiB/s. This is a workload-shape effect, not a silicon claim.

A second discovery is that blindly adding frame-parallel nodes can increase context replication. In the 5-fps raw-gather case, 16 nodes require 400 MiB/s on the tested grid while 4/8 nodes cross at 200 MiB/s. F4 must therefore schedule for temporal-context residency, not only available NPU capacity.

## F1 compute envelope

Formula required by the campaign: `2 TOPS × node_count × effective_utilization / reconstructed_positions_per_second`. The table below uses 40% effective utilization and also subtracts the synthetic 12 GOP cost assigned to each genuine source frame.

| source fps | nodes | simple max GOP/recon | after keyframe finish | synthetic need | total synthetic TOPS |
|---:|---:|---:|---:|---:|---:|
| 5 | 4 | 58.2 | 57.1 | 30.0 | 1.710 |
| 5 | 8 | 116.4 | 115.3 | 30.0 | 1.710 |
| 5 | 16 | 232.7 | 231.6 | 30.0 | 1.710 |
| 10 | 4 | 64.0 | 61.6 | 30.0 | 1.620 |
| 10 | 8 | 128.0 | 125.6 | 30.0 | 1.620 |
| 10 | 16 | 256.0 | 253.6 | 30.0 | 1.620 |
| 15 | 4 | 71.1 | 67.1 | 30.0 | 1.530 |
| 15 | 8 | 142.2 | 138.2 | 30.0 | 1.530 |
| 15 | 16 | 284.4 | 280.4 | 30.0 | 1.530 |

At 4 nodes, the synthetic workload requires about 1.71 TOPS for 5-fps input, 1.62 TOPS for 10-fps input, and 1.53 TOPS for 15-fps input. Nominal 8 TOPS is therefore not the useful number; at 20% effective utilization the four-chip fabric delivers only 1.6 effective TOPS. The utilization sensitivity sweep correctly fails 5-fps and 10-fps four-node cases at 20% while the 15-fps case passes.

## Bytes

- One 360p YUV420 frame: 345600 bytes (0.3296 MiB).
- One raw 1080p YUV420 frame: 3110400 bytes (2.9663 MiB), or 178.0 MiB/s at 60 fps before protocol overhead.
- Assumed local encoded output: 12 Mbit/s = 25000 bytes/display frame = 1.43 MiB/s.
- Assumed sidecar: 65536 bytes per cached authoritative generation.

At the 360p10 / 4-node / 40% / 125-MiB/s encoded point, the cache-aware scheduler moves about 292 KiB per displayed frame and 17.1 MiB/s over the ten-second clip. Raw 1080p gathering adds roughly 178 MiB/s and is therefore already beyond a 1-GbE-class payload envelope before practical overhead.

## Facts vs assumptions

**DATASHEET / PRIMARY-SOURCE INPUTS**
- Original RV1126: quad Cortex-A7 + RISC-V MCU; 2.0 TOPS NPU with INT8/INT16 support; 2D scale up/down; 4K H.264/H.265 encode/decode; RGMII; USB 2.0; dual SDIO 3.0. Source: Rockchip RV1126 product page.
- Original RV1126 belongs to RKNN-Toolkit/RKNPU, while RK3566/RK3568/RK3588 belong to RKNN-Toolkit2/RKNPU2. Source: airockchip RKNN-Toolkit and RKNN-Toolkit2 repositories.
- Rockchip MPP lists RV1109/RV1126 among supported hardware platforms. Source: rockchip-linux/mpp.

**ASSUMED FOR F0/F1 ONLY**
- Delivered NPU throughput is nominal 2 TOPS multiplied by a swept 20–70% effective-utilization factor.
- 30 GOP per reconstructed display frame: 18 GOP warp/refine + 12 GOP finishing; 12 GOP finishing for genuine source frames.
- 64 KiB sidecar per authoritative generation; four-generation temporal cache per RV node.
- 12 Mbit/s local H.265-like encoded output alternative; raw gather alternative is YUV420.
- 0.6 ms aggregate NPU submission/pre/post overhead per task; 0.08 ms link transaction latency.
- Full-duplex shared controller link; no packet/protocol loss and no RK3588 saturation yet.
- 250 ms offline playback buffer. This is not a live-world latency assumption.

**NOT YET CLAIMED**
- That a 30-GOP interpolation/SR graph exists for RV1126.
- That its operations are all old-RKNN NPU-native.
- Silicon memory bandwidth, NPU timing, thermal sustain, Ethernet payload efficiency, MPP multi-stream capacity, visual quality, or GPU savings.

## Exact next experiment — F2

Build an operator-contract matrix from the original RKNN-Toolkit/RKNPU documentation and compile a deliberately tiny RV1126-oriented residual/interpolation graph. Every op is classified NPU_NATIVE / CPU_FALLBACK / REWRITE / PRECOMPUTE_UPSTREAM / DETERMINISTIC_HARDWARE / FATAL. The first executable discriminator is not image quality: it is whether a fixed-shape INT8 graph made only of confirmed old-RKNN-supported primitives converts without hidden CPU fallback. Only after that passes should its measured/estimated GOP count replace the F0 synthetic 30-GOP budget.
