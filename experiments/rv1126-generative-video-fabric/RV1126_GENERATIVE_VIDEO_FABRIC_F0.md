# RV1126 GENERATIVE VIDEO FABRIC — F0 + F1
Status: **software workload model internally consistent after one rejected model bug and rerun**. This is not RV1126 silicon timing proof.
## Purpose
Bounded first experiment for an RK3588-controlled pool of original RV1126 SoCs used as a cheap temporal/spatial video expansion decoder. The model extends the existing `frankenfabric_f02.py` concepts: explicit owner/generation/checksum state, stale rejection, node queues, transfer cost and separate ingress/egress contention.
## Primary-source facts used
- **DATASHEET:** original RV1126: quad Cortex-A7 + RISC-V MCU; 2.0 TOPS NPU with INT8/INT16; 2D scale up/down; 4K H.264/H.265 30 fps encode/decode. Source: Rockchip RV1126 product page.
- **SOFTWARE:** original RV1126 belongs to RKNN-Toolkit/RKNPU, not the RKNN-Toolkit2/RKNPU2 path used by RK356x/RK3588. Sources: airockchip/rockchip-linux RKNN repositories.
- **SOFTWARE:** Rockchip MPP explicitly lists RV1109/RV1126 and provides hardware codec abstraction.
- **PROVEN ELSEWHERE IN PROJECT:** generation-tagged 8192-byte mutable state transfer/rejection is already proven by V9; F0 only reuses the contract and does not re-prove the physical byte copy.
## F0 workload
10-second target output, 1080p60 = 600 display positions. Source rates 5/10/15 fps. Each authoritative position carries a 360p YUV420-equivalent keyframe plus an assumed sidecar. Non-authoritative positions carry compact temporal dispatch data and consume an assumed NPU GOP budget. Output is modeled as an encoded frame payload, not raw 1080p transport.
### Explicit F0 assumptions
- **ASSUMED:** `operator_efficiency` = `0.85`
- **ASSUMED:** `quantization_efficiency` = `0.9`
- **ASSUMED:** `thermal_factor` = `0.95`
- **ASSUMED:** `model_GOP_per_reconstructed` = `40`
- **ASSUMED:** `keyframe_360p_bytes` = `345600`
- **ASSUMED:** `sidecar_bytes_per_authoritative` = `180000`
- **ASSUMED:** `dispatch_bytes_per_reconstructed` = `120000`
- **ASSUMED:** `encoded_output_bytes_per_frame` = `20000`
- **ASSUMED:** `full_duplex_interconnect` = `True`
- **ASSUMED:** secondary delivered-TOPS derates are operator 0.85 × quantization 0.90 × thermal 0.95. These are sensitivity factors, not RV1126 measurements.
- **ASSUMED:** full-duplex fabric link with independent ingress and egress queueing. The first run incorrectly serialized both directions and was rejected; the repaired run uses separate clocks, matching `frankenfabric_f02` semantics.
- **NOT MODELED YET:** image quality, real RKNN layer timings, DRAM bandwidth, CPU fallback, RK3588 assembly saturation, MPP queue limits, tile halo/seam cost, packet loss, thermal dynamics.
## F0 baseline — 40% utilization, 40 GOP/reconstructed frame, 1 GbE
| Source→output | RV nodes | Recon fps | Effective TOPS after derates | Required TOPS | Compute load | Link load | Throughput | P95 pipeline latency | Immediate 16.7ms misses |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 360p5→1080p60 | 4 | 55 | 2.326 | 2.200 | 0.946 | 0.074 | 59.65 fps | 74.8 ms | 99.8% |
| 360p5→1080p60 | 8 | 55 | 4.651 | 2.200 | 0.473 | 0.074 | 59.68 fps | 70.3 ms | 99.8% |
| 360p5→1080p60 | 16 | 55 | 9.302 | 2.200 | 0.236 | 0.074 | 59.68 fps | 70.3 ms | 99.8% |
| 360p10→1080p60 | 4 | 50 | 2.326 | 2.000 | 0.860 | 0.090 | 59.67 fps | 72.6 ms | 99.8% |
| 360p10→1080p60 | 8 | 50 | 4.651 | 2.000 | 0.430 | 0.090 | 59.68 fps | 70.3 ms | 99.8% |
| 360p10→1080p60 | 16 | 50 | 9.302 | 2.000 | 0.215 | 0.090 | 59.68 fps | 70.3 ms | 99.8% |
| 360p15→1080p60 | 4 | 45 | 2.326 | 1.800 | 0.774 | 0.106 | 59.68 fps | 70.3 ms | 99.8% |
| 360p15→1080p60 | 8 | 45 | 4.651 | 1.800 | 0.387 | 0.106 | 59.68 fps | 70.3 ms | 99.8% |
| 360p15→1080p60 | 16 | 45 | 9.302 | 1.800 | 0.193 | 0.106 | 59.68 fps | 70.3 ms | 99.8% |

Interpretation: aggregate throughput and single-frame latency are different. Four nodes can pipeline a 40-GOP/frame workload at roughly the 60-fps target when the assumed delivered compute is adequate, but one frame still spends ~69 ms in one NPU job. Offline finishing can prebuffer; a live-world display cannot call this 16.7-ms compliant.
## Bandwidth crossover under the F0 byte model
| Source fps | Ingress MB/s | Egress MB/s | Minimum full-duplex link | +20% headroom | Avg fabric bytes/display frame |
|---:|---:|---:|---:|---:|---:|
| 5 | 9.228 | 1.200 | 73.8 Mb/s | 88.6 Mb/s | 173.8 kB |
| 10 | 11.256 | 1.200 | 90.0 Mb/s | 108.1 Mb/s | 207.6 kB |
| 15 | 13.284 | 1.200 | 106.3 Mb/s | 127.5 Mb/s | 241.4 kB |

This makes 100 Mbit/s a real crossover in the assumed representation: 360p5 has margin, 360p10 is marginal once headroom is required, and 360p15 exceeds 100 Mbit/s before protocol overhead. 1 GbE has large headroom for this *frame-parallel compact-dispatch* design. It would not have large headroom for raw 1080p stage-to-stage tensors.
## F1 compute envelope
The requested nominal formula is `2 TOPS × nodes × effective_utilization / reconstructed_fps`. The simulator also exposes a stricter derated budget after operator/quantization/thermal factors.
| Source fps | Nodes | Recon fps | Max GOP/frame @20% derated | @40% derated | @70% derated |
|---:|---:|---:|---:|---:|---:|
| 5 | 4 | 55 | 21.14 | 42.28 | 74.00 |
| 5 | 8 | 55 | 42.28 | 84.57 | 147.99 |
| 5 | 16 | 55 | 84.57 | 169.13 | 295.99 |
| 10 | 4 | 50 | 23.26 | 46.51 | 81.40 |
| 10 | 8 | 50 | 46.51 | 93.02 | 162.79 |
| 10 | 16 | 50 | 93.02 | 186.05 | 325.58 |
| 15 | 4 | 45 | 25.84 | 51.68 | 90.44 |
| 15 | 8 | 45 | 51.68 | 103.36 | 180.88 |
| 15 | 16 | 45 | 103.36 | 206.72 | 361.76 |
## Live-world latency crossover
For 16.667-ms display service, the ideal compute ceiling is much smaller unless a frame is split across NPUs. With 0.15-ms submission overhead and perfect tile parallelism, ignoring all transfer/halo/seam cost:
| Utilization | 1 tile/NPU | 2-way | 4-way | 8-way |
|---:|---:|---:|---:|---:|
| 20% | 4.80 GOP | 9.60 | 19.21 | 38.41 |
| 40% | 9.60 GOP | 19.21 | 38.41 | 76.82 |
| 70% | 16.81 GOP | 33.61 | 67.22 | 134.44 |

At the median 40% assumption, a whole-frame 40-GOP network is **not** a live 16.7-ms design. Ideal four-way tile split reaches only ~38.4 GOP before communication/halo/seam overhead; eight-way reaches ~76.8 GOP. This is the strongest immediate architectural result from F0/F1.
## Bottleneck classification
- **Offline, 4 nodes, 40 GOP/frame, 40% delivered-utilization assumption:** compute is near saturation at 5 fps source (94.6% load), less constrained at 10/15 fps. Link is not the bottleneck at 1 GbE.
- **100 Mbit/s:** representation bandwidth becomes the first bottleneck by 15-fps source and is uncomfortably close at 10 fps.
- **Live world:** per-frame NPU latency is the bottleneck even when aggregate throughput is sufficient. Tile parallelism or a much smaller residual network is mandatory.
- **Stage-pipeline warning:** moving a raw 1080p YUV420 intermediate is 3,110,400 bytes/frame. One such transfer at 50 reconstructed fps is ~155.5 MB/s before any second stage, already beyond 1 GbE. Therefore F0 favors frame-local work / compact sidecars and predicts that naive stage-pipelining across Ethernet is structurally bad.
## Generation / stale-state contract
Every display position has a monotonic generation, explicit owner and deterministic checksum. Each run injects one delayed older generation after a newer generation is accepted. All sweep cases require exactly one stale-generation rejection and zero duplicate-authoritative-owner errors. This is a software-model invariant, not a new V9 proof.
## Facts vs assumptions
**FACT/DATASHEET:** 2 TOPS NPU, Cortex-A7/RISC-V MCU, video codec, 2D scaling, listed interfaces.

**FACT/SOFTWARE:** legacy RKNN-Toolkit/RKNPU path for original RV1126; MPP support.

**PROJECT-PROVEN:** V9 generation-tagged 8192-byte state semantics exist independently.

**ASSUMED:** effective utilization; operator/quant/thermal derates; keyframe/sidecar/dispatch/output sizes; 40 GOP baseline; full-duplex network efficiency; negligible controller saturation.

**TARGET:** convincing 1080p60 quality and useful GPU-dollar reduction. F0/F1 make no quality or GPU-economics claim.
## F2 started — operator-realistic network gate
Original RV1126 must use legacy RKNN-Toolkit/RKNPU. The current repository documentation includes RKNN-Toolkit v1.7.5 manuals, but the exact legacy operator restriction matrix has not yet been machine-extracted here. Therefore F2 starts conservatively rather than assuming modern Toolkit2 support.
Candidate network rule until converter proof: convolutional residual blocks only; deterministic warp/resize kept outside the NPU when possible; no attention/transformer dependency; no GridSample dependency; no dynamic-shape dependency. Modern Toolkit2's own operator table marks GridSample unsupported, which is an additional warning, not evidence about legacy RV1126.
### Exact next experiment
1. Build a tiny fixed-shape INT8 residual CNN using only Conv + simple activation + Add/Concat candidates.
2. Export ONNX at a legacy-compatible opset and run it through RKNN-Toolkit v1.7.5 targeting `rv1126` in a controlled conversion environment.
3. Parse converter output layer-by-layer into `NPU native / CPU fallback / rewrite / deterministic hardware / fatal`.
4. Deliberately add Resize and then a warp/GridSample-like operation as negative controls; require the audit to expose fallback/rejection rather than hiding it.
5. Feed the accepted graph's actual MAC/GOP count back into F1 and rerun the 4/8/16-node throughput/latency envelope.
6. Only after graph conversion passes, add image-quality reconstruction against a downsampled/frame-dropped 1080p60 ground-truth clip.
