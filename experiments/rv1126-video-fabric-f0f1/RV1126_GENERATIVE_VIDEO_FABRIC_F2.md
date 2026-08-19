# RV1126 GENERATIVE VIDEO FABRIC — F2

Status: **OFFLINE RKNN TOOLKIT 1.7.5 COMPILER / OPERATOR-CONTRACT PASS FOR TARGET `rv1126`**.

This is not RV1126 silicon timing proof, not proof of sustained NPU utilization, and not proof that every compiled primitive executes with the desired hardware efficiency. It proves that a deliberately constrained fixed-shape residual graph can be imported, optimized, built and exported by the original RV1126 RKNN toolchain in both non-quantized and INT8-quantized form.

## Exact graph

Input/output probe shape: `1x3x64x64`.

```text
pre-warped RGB
  -> Conv 3x3 3->16
  -> ReLU
  -> grouped/depthwise Conv 3x3 16 channels
  -> ReLU
  -> Conv 1x1 16->3
  -> Add(original input, residual)
  -> corrected RGB
```

Geometric optical-flow warp / reprojection is intentionally **outside** this NPU graph. `grid_sample` was not found in the published RKNN 1.7.5 operator-support lists, so F2 treats it as forbidden rather than risking Cortex-A7 fallback.

## Evidence

GitHub Actions frozen PASS commit:

`af5d9118eae79220c3253506517542d8f0abbb57`

The pass required:

1. exact official RKNN Toolkit 1.7.5 x86_64 Python 3.8 wheel;
2. `target_platform=['rv1126']`;
3. successful ONNX import and optimization;
4. successful non-quantized `build()` and `.rknn` export;
5. successful INT8 quantization `build()` using deterministic JPEG calibration images and `.rknn` export.

The compiler log explicitly maps the graph to RKNN internal `convolution`, `relu`, and `add` operators and later packs all three convolution layers. The INT8 path executes activation-range analysis across the graph before producing the quantized artifact.

Frozen artifact SHA-256 values:

- ONNX: `ab45c6b60a4ac6ae15b635a1903b31a59a17024b8381a188d156f1c354861631`
- FP RKNN: `e2fd2e44c580588ed13674ed31a1333ae0a9351c7f60cd034ecfad99db9302f1`
- INT8 RKNN: `2eddfdaadf88f2c381c58cb66b7c5ad33ef010a8adcf48731788d2970d190f97`
- compiler log: `598ab52d990d1a802e9fc3d9d95e530e3d9b7087b591b5a7502f78ad7e33a43a`

## Failure chain retained as evidence

The campaign deliberately retained its failed probes instead of hiding them:

1. missing TensorFlow: RKNN 1.7.5 imports its TensorFlow-backed Acuity layer even for this ONNX path;
2. missing PyTorch: RKNN optimizer imports model-pruning support during initialization;
3. FP build with no dataset: old toolkit input-meta path failed; Rockchip examples also provide `dataset.txt` for non-quantized builds;
4. PPM calibration: FP exported but INT8 rejected the calibration-file format;
5. JPEG calibration: both FP and INT8 exported successfully.

These were harness/environment-contract failures, not evidence that the selected graph operators were unsupported.

## Compute arithmetic

Counting one MAC as two operations, this exact channel topology scales approximately with image pixel count as follows:

| graph resolution | operations/frame | share of F0 synthetic 30-GOP budget |
|---|---:|---:|
| 360p | 0.288 GOP | 0.96% |
| 540p | 0.647 GOP | 2.16% |
| 720p | 1.150 GOP | 3.83% |
| 1080p | 2.588 GOP | 8.63% |

This does **not** mean the graph can produce acceptable reconstruction quality. Its current weights are deterministic test weights, not trained video-repair weights. The table establishes only that the chosen operator topology leaves very large arithmetic headroom inside F0's deliberately conservative 30-GOP reconstruction allowance.

## Operator policy carried forward

- `Conv`, grouped/depthwise `Conv`, `ReLU`, fixed-shape residual `Add`: compiler-accepted F2 primitives.
- Dense optical-flow warp / reprojection: deterministic/controller/video-hardware path, not this NPU graph.
- Coarse scale: deterministic 2D hardware candidate; do not burn NPU arithmetic on pure resizing without evidence.
- Dynamic shapes, attention, arbitrary transformer blocks: excluded from F2.
- Any important graph operation that requires Cortex-A7 fallback remains a failure condition for the intended product architecture.

## Exact next experiment — F3

Build the first complete reconstruction-cost pipeline without claiming quality:

1. source/keyframe state at 360p/540p/720p;
2. deterministic warp/reprojection;
3. compiler-accepted tiny residual correction;
4. deterministic 2x/coarse scaling where useful;
5. a second compiler-compatible residual/SR correction stage at the intermediate resolution;
6. explicit alternatives for output transport: raw gather, finishing-node ownership, and independently encoded access units with bitrate penalty;
7. sweep residual-refinement pixel fraction and disocclusion fraction rather than applying the neural network to every pixel unconditionally.

F3 must replace the single synthetic `30 GOP/reconstructed frame` knob with component-level arithmetic and must preserve the F0/F1 deadline, bandwidth, residency and stale-generation accounting.
