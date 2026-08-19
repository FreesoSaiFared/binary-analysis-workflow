# RV1126 GENERATIVE VIDEO FABRIC — F3

Status: **COMPONENT-LEVEL SOFTWARE SIMULATION PASS**. This is not RV1126 silicon timing, trained-model quality, memory-bandwidth, MPP throughput, or NPU-utilization proof.

F3 replaces F0/F1's single synthetic 30-GOP reconstructed-frame knob with component arithmetic derived from the exact F2 compiler-accepted residual topology plus explicit deterministic warp/scale latency assumptions.

## Median 360p scene, 4-node module, 40% effective-NPU factor, 125 MiB/s full-duplex controller link

| input | transport | fps | misses | P95 latency ms | transfer MiB/s | compute capacity | bottleneck |
|---|---|---:|---:|---:|---:|---:|---|
| 360p5 | owner_only | 60.73 | 0 | 43.0 | 3.4 | 9.40x | HEADROOM |
| 360p5 | finishing_owner | 60.09 | 0 | 97.1 | 61.8 | 37.58x | HEADROOM |
| 360p5 | independent_au | 61.04 | 0 | 20.0 | 10.4 | 37.58x | HEADROOM |
| 360p5 | raw_gather | 41.47 | 592 | 4178.5 | 185.8 | 37.58x | INTERCONNECT |
| 360p10 | owner_only | 60.36 | 0 | 22.9 | 5.4 | 9.56x | HEADROOM |
| 360p10 | finishing_owner | 60.15 | 0 | 42.8 | 52.7 | 38.24x | HEADROOM |
| 360p10 | independent_au | 60.49 | 0 | 13.8 | 14.3 | 38.24x | HEADROOM |
| 360p10 | raw_gather | 41.76 | 579 | 4130.9 | 189.7 | 38.24x | INTERCONNECT |
| 360p15 | owner_only | 60.24 | 0 | 16.2 | 7.3 | 9.73x | HEADROOM |
| 360p15 | finishing_owner | 60.08 | 0 | 31.7 | 58.4 | 38.93x | HEADROOM |
| 360p15 | independent_au | 60.31 | 0 | 12.7 | 20.1 | 38.93x | HEADROOM |
| 360p15 | raw_gather | 41.85 | 575 | 4111.9 | 195.5 | 38.93x | INTERCONNECT |

The central F3 result is structural: under this deliberately tiny compiler-accepted neural topology, arithmetic is not the modeled bottleneck for one stream. Transport and ownership dominate much earlier. `raw_gather` exceeds the 125 MiB/s test point and misses nearly the entire clip, while owner-local encoded output, a finishing owner, and the penalized independently-decodable-unit model all meet the 250 ms offline deadline in the median cases.

## Component arithmetic

| source | reconstructed frame GOP | genuine frame GOP |
|---|---:|---:|
| 360p | 1.443447 | 1.150157 |
| 540p | 1.810059 | 1.150157 |
| 720p | 2.323317 | 1.150157 |

For the median 360p workload the reconstructed-frame neural arithmetic is 1.443446784 GOP and genuine-frame finishing is 1.1501568 GOP. These are graph arithmetic under assumed refinement fractions/passes, not measured NPU execution times.

## Bandwidth crossover — median 360p, 4 nodes, 40% effective factor

| input | owner-only | finishing-owner | independent AU | raw gather |
|---|---:|---:|---:|---:|
| 360p5 | 12.5 | 50 | 12.5 | 200 |
| 360p10 | 12.5 | 12.5 | 12.5 | 200 |
| 360p15 | 12.5 | 12.5 | 12.5 | 200 |

The 5-fps finishing-owner case retains F0's burst effect and needs 50 MiB/s on the tested grid because many reconstructed positions become eligible together and non-owner workers return 720p intermediates. Raw 1080p gathering requires 200 MiB/s across all three source rates on the tested grid.

## Facts, assumptions, and boundaries

**COMPILER-CONTRACT EVIDENCE**
- F2 successfully exported both FP and INT8 `.rknn` artifacts for target `rv1126` from the fixed Conv/ReLU/depthwise-Conv/ReLU/Conv/Add residual graph.
- F3 uses the exact arithmetic of that topology: 1248 operations/pixel when one MAC is counted as two operations.

**ASSUMED F3 WORKLOAD PARAMETERS**
- optimistic/median/adversarial fractions of pixels needing residual refinement and disocclusion repair;
- one to four residual passes depending on scene profile;
- deterministic warp and scale latency per megapixel;
- 12 Mbit/s base encoded stream; independent-access-unit bitrate penalties of 1.35x / 1.8x / 3x;
- finishing-owner transport sends raw 720p intermediates from non-owner workers;
- 20–70% nominal-TOPS effectiveness remains a sensitivity parameter, not measured efficiency;
- ingress and egress are independent full-duplex queues as in F0.

**NOT PROVEN**
- that these refinement fractions produce acceptable images;
- RV1126 NPU execution time, DRAM bandwidth or thermal sustain;
- that MPP can provide the modeled independent-access-unit or finishing-owner encoding topology at the assumed bitrate;
- cross-node codec-state assembly;
- actual GPU compute savings;
- shared/half-duplex switch behavior.

## Adverse implication

Adding RV1126s is not automatically beneficial for one stream. With a tiny residual graph, frame-parallel workers replicate temporal context and can create more transport. The economically relevant use of extra chips may therefore be **more simultaneous streams**, larger quality networks, or specialized pools—not simply lower latency for one stream.

## Exact next experiment — F4

Compare `owner/residency`, naive frame-parallel, stage-pipeline, tile-parallel, and V9-like adaptive scheduling under the F3 component workload. Include temporal-context replication, tile halos/seams, finishing-owner traffic, and multi-stream packing. F4 must identify when a four-chip module should accelerate one stream versus run four largely independent stream owners.
