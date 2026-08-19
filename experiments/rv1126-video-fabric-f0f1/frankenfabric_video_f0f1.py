#!/usr/bin/env python3
from __future__ import annotations
from dataclasses import dataclass, field
from collections import OrderedDict
from hashlib import sha256
from pathlib import Path
import argparse, csv, json, math, statistics

MIB = 1024 * 1024
OUT_FPS = 60
DURATION_S = 10
OUTPUT_FRAMES = OUT_FPS * DURATION_S
LOW_W, LOW_H = 640, 360
OUT_W, OUT_H = 1920, 1080
YUV420_BPP = 1.5
LOW_FRAME_BYTES = int(LOW_W * LOW_H * YUV420_BPP)
OUT_FRAME_BYTES = int(OUT_W * OUT_H * YUV420_BPP)
SIDECAR_BYTES = 64 * 1024
ENCODED_BITRATE_MBPS = 12.0
ENCODED_FRAME_BYTES = int(ENCODED_BITRATE_MBPS * 1_000_000 / 8 / OUT_FPS)
NOMINAL_TOPS_PER_NODE = 2.0
KEYFRAME_FINISH_GOP = 12.0
RECON_WARP_REFINE_GOP = 18.0
RECON_FINISH_GOP = 12.0
RECON_TOTAL_GOP = RECON_WARP_REFINE_GOP + RECON_FINISH_GOP
SUBMIT_OVERHEAD_MS = 0.25
PREPOST_MS = 0.35
LINK_LATENCY_MS = 0.08
CACHE_GENERATIONS = 4
PLAYBACK_BUFFER_MS = 250.0
SOURCE_FPS_VALUES = (5, 10, 15)
NODE_COUNTS = (4, 8, 16)
UTIL_VALUES = (0.20, 0.30, 0.40, 0.50, 0.60, 0.70)
BW_VALUES = (12.5, 25, 50, 100, 125, 200, 400, 800, 1600)

@dataclass
class Stream:
    name: str = 'offline-360p-to-1080p60'
    output_fps: int = OUT_FPS

@dataclass
class FrameGeneration:
    generation: int
    checksum: str

@dataclass
class AuthoritativeFrame(FrameGeneration):
    source_fps: int

@dataclass
class PredictedFrame(FrameGeneration):
    prev_authoritative: int
    next_authoritative: int

@dataclass
class Tile:
    frame_generation: int
    tile_index: int = 0
    tile_count: int = 1

@dataclass
class TemporalWindow:
    generations: tuple[int, ...]

@dataclass
class ModelResidency:
    names: tuple[str, ...] = ('warp_refine_tiny', 'sr_finish_tiny')

@dataclass
class SidecarState:
    generation: int
    bytes: int = SIDECAR_BYTES

@dataclass
class DecoderState:
    owner: int = 0
    authoritative_generation: int = -1
    checksum: str = ''
    stale_replays_rejected: int = 0
    def accept_authoritative(self, generation: int, payload: bytes) -> bool:
        if generation <= self.authoritative_generation:
            self.stale_replays_rejected += 1
            return False
        self.authoritative_generation = generation
        self.checksum = sha256(payload).hexdigest()
        return True

@dataclass
class DisplayDeadline:
    frame_generation: int
    deadline_ms: float

@dataclass
class CorrectionDeadline:
    authoritative_generation: int
    deadline_ms: float

@dataclass
class Node:
    node_id: int
    compute_available_ms: float = 0.0
    busy_ms: float = 0.0
    cache: OrderedDict[int, bool] = field(default_factory=OrderedDict)
    def touch_generation(self, generation: int) -> None:
        if generation in self.cache:
            self.cache.move_to_end(generation)
        else:
            self.cache[generation] = True
            while len(self.cache) > CACHE_GENERATIONS:
                self.cache.popitem(last=False)

def frame_task(frame_index: int, source_fps: int) -> dict:
    stride = OUT_FPS // source_fps
    authoritative_count = source_fps * DURATION_S
    is_authoritative = (frame_index % stride) == 0
    prev_gen = min(frame_index // stride, authoritative_count - 1)
    next_gen = min(prev_gen + 1, authoritative_count - 1)
    if is_authoritative:
        contexts = (prev_gen,)
        release_ms = prev_gen * (1000.0 / source_fps)
        gop = KEYFRAME_FINISH_GOP
        kind = 'authoritative'
    else:
        contexts = tuple(dict.fromkeys((prev_gen, next_gen)))
        release_ms = next_gen * (1000.0 / source_fps)
        gop = RECON_TOTAL_GOP
        kind = 'reconstructed'
    display_ms = frame_index * (1000.0 / OUT_FPS)
    return {
        'frame_index': frame_index,
        'kind': kind,
        'contexts': contexts,
        'release_ms': release_ms,
        'display_ms': display_ms,
        'deadline_ms': display_ms + PLAYBACK_BUFFER_MS,
        'gop': gop,
    }

def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(p * len(ordered)) - 1))
    return ordered[idx]

def classify_bottleneck(result: dict) -> str:
    if result['compute_headroom_ratio'] < 1.0:
        return 'COMPUTE_CAPACITY'
    if result['deadline_misses'] and result['ingress_busy_fraction'] >= 0.80:
        return 'OUTPUT_INTERCONNECT'
    if result['deadline_misses'] and result['egress_busy_fraction'] >= 0.80:
        return 'INPUT_INTERCONNECT'
    if result['deadline_misses']:
        return 'PER_FRAME_LATENCY_OR_BURST'
    if result['ingress_busy_fraction'] >= 0.70:
        return 'OUTPUT_INTERCONNECT_HEADROOM_LOW'
    if result['egress_busy_fraction'] >= 0.70:
        return 'INPUT_INTERCONNECT_HEADROOM_LOW'
    if result['compute_headroom_ratio'] < 1.25:
        return 'COMPUTE_HEADROOM_LOW'
    return 'HEADROOM'

def simulate(source_fps: int, nodes_n: int, utilization: float, bandwidth_mib_s: float,
             output_mode: str = 'encoded') -> dict:
    nodes = [Node(i) for i in range(nodes_n)]
    controller_egress_available_ms = 0.0
    controller_egress_busy_ms = 0.0
    input_bytes = 0
    output_requests = []
    delivered_tops_per_node = NOMINAL_TOPS_PER_NODE * utilization
    tasks = sorted((frame_task(i, source_fps) for i in range(OUTPUT_FRAMES)),
                   key=lambda t: (t['release_ms'], t['frame_index']))
    for task in tasks:
        best = None
        for node in nodes:
            missing = [g for g in task['contexts'] if g not in node.cache]
            in_bytes = len(missing) * (LOW_FRAME_BYTES + SIDECAR_BYTES)
            in_start = max(task['release_ms'], controller_egress_available_ms)
            in_duration = (LINK_LATENCY_MS + (in_bytes / MIB) / bandwidth_mib_s * 1000.0) if in_bytes else 0.0
            in_done = in_start + in_duration
            compute_start = max(in_done, node.compute_available_ms)
            compute_duration = task['gop'] / delivered_tops_per_node + SUBMIT_OVERHEAD_MS + PREPOST_MS
            compute_done = compute_start + compute_duration
            candidate = (compute_done, len(missing), node.node_id, in_bytes,
                         in_start, in_duration, compute_start, compute_duration)
            if best is None or candidate < best:
                best = candidate
        compute_done, _, node_id, in_bytes, in_start, in_duration, compute_start, compute_duration = best
        node = nodes[node_id]
        if in_bytes:
            controller_egress_available_ms = in_start + in_duration
            controller_egress_busy_ms += in_duration
            input_bytes += in_bytes
        for generation in task['contexts']:
            node.touch_generation(generation)
        node.compute_available_ms = compute_done
        node.busy_ms += compute_duration
        output_requests.append((compute_done, task))

    controller_ingress_available_ms = 0.0
    controller_ingress_busy_ms = 0.0
    output_bytes = 0
    output_frame_bytes = ENCODED_FRAME_BYTES if output_mode == 'encoded' else OUT_FRAME_BYTES
    completed = []
    for ready_ms, task in sorted(output_requests, key=lambda x: (x[0], x[1]['frame_index'])):
        start_ms = max(ready_ms, controller_ingress_available_ms)
        duration_ms = LINK_LATENCY_MS + (output_frame_bytes / MIB) / bandwidth_mib_s * 1000.0
        done_ms = start_ms + duration_ms
        controller_ingress_available_ms = done_ms
        controller_ingress_busy_ms += duration_ms
        output_bytes += output_frame_bytes
        completed.append((task, done_ms))

    latencies = [done - task['release_ms'] for task, done in completed]
    misses = sum(done > task['deadline_ms'] + 1e-9 for task, done in completed)
    final_done_ms = max(done for _, done in completed)
    first_release_ms = min(task['release_ms'] for task, _ in completed)

    decoder = DecoderState()
    assert decoder.accept_authoritative(104, b'world-generation-104')
    assert decoder.accept_authoritative(105, b'world-generation-105')
    assert not decoder.accept_authoritative(104, b'late-world-generation-104')
    assert decoder.stale_replays_rejected == 1

    reconstructed_per_second = OUT_FPS - source_fps
    required_gop_s = reconstructed_per_second * RECON_TOTAL_GOP + source_fps * KEYFRAME_FINISH_GOP
    delivered_gop_s = 2000.0 * nodes_n * utilization
    result = {
        'source_resolution': '360p',
        'source_fps': source_fps,
        'output_resolution': '1080p',
        'output_fps_target': OUT_FPS,
        'nodes': nodes_n,
        'nominal_tops_per_node': NOMINAL_TOPS_PER_NODE,
        'effective_utilization': utilization,
        'bandwidth_mib_s': bandwidth_mib_s,
        'output_mode': output_mode,
        'frames': OUTPUT_FRAMES,
        'authoritative_frames': source_fps * DURATION_S,
        'reconstructed_frames': OUTPUT_FRAMES - source_fps * DURATION_S,
        'throughput_fps': OUTPUT_FRAMES / ((final_done_ms - first_release_ms) / 1000.0),
        'p50_latency_ms': statistics.median(latencies),
        'p95_latency_ms': percentile(latencies, 0.95),
        'p99_latency_ms': percentile(latencies, 0.99),
        'deadline_misses': misses,
        'input_bytes': input_bytes,
        'output_bytes': output_bytes,
        'total_transfer_bytes': input_bytes + output_bytes,
        'avg_transfer_bytes_per_frame': (input_bytes + output_bytes) / OUTPUT_FRAMES,
        'transfer_mib_s_over_clip': (input_bytes + output_bytes) / MIB / DURATION_S,
        'egress_busy_fraction': controller_egress_busy_ms / final_done_ms,
        'ingress_busy_fraction': controller_ingress_busy_ms / final_done_ms,
        'max_node_busy_fraction': max(n.busy_ms / final_done_ms for n in nodes),
        'stale_replays_rejected': decoder.stale_replays_rejected,
        'required_gop_s': required_gop_s,
        'required_effective_tops': required_gop_s / 1000.0,
        'delivered_gop_s': delivered_gop_s,
        'compute_headroom_ratio': delivered_gop_s / required_gop_s,
    }
    result['bottleneck'] = classify_bottleneck(result)
    return result

def f1_envelope(source_fps: int, nodes_n: int, utilization: float) -> dict:
    recon_fps = OUT_FPS - source_fps
    delivered_gop_s = 2000.0 * nodes_n * utilization
    simple_max = delivered_gop_s / recon_fps
    residual_max = (delivered_gop_s - source_fps * KEYFRAME_FINISH_GOP) / recon_fps
    total_required_gop_s = recon_fps * RECON_TOTAL_GOP + source_fps * KEYFRAME_FINISH_GOP
    return {
        'source_fps': source_fps,
        'reconstructed_positions_per_s': recon_fps,
        'nodes': nodes_n,
        'nominal_aggregate_tops': NOMINAL_TOPS_PER_NODE * nodes_n,
        'effective_utilization': utilization,
        'effective_aggregate_tops': NOMINAL_TOPS_PER_NODE * nodes_n * utilization,
        'simple_max_gop_per_reconstructed_frame': simple_max,
        'max_gop_per_reconstructed_frame_after_keyframe_finish': residual_max,
        'synthetic_required_gop_per_reconstructed_frame': RECON_TOTAL_GOP,
        'synthetic_keyframe_finish_gop': KEYFRAME_FINISH_GOP,
        'synthetic_total_required_tops': total_required_gop_s / 1000.0,
        'capacity_margin': delivered_gop_s / total_required_gop_s,
        'capacity_pass': delivered_gop_s + 1e-9 >= total_required_gop_s,
    }

def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

def fmt(x, n=2):
    return f'{x:.{n}f}'

def build_report(summary: dict) -> str:
    lines = []
    lines.append('# RV1126 GENERATIVE VIDEO FABRIC — F0/F1')
    lines.append('')
    lines.append('Status: SOFTWARE WORKLOAD / CONTRACT SIMULATION. Not RV1126 timing proof. QEMU ARM `virt` is not used here and would not be RV1126 silicon emulation.')
    lines.append('')
    lines.append('## F0 result at the declared median point')
    lines.append('')
    lines.append('Median point: 40% effective NPU utilization, 125 MiB/s full-duplex controller link, local 12 Mbit/s encoded output, 250 ms offline playback buffer.')
    lines.append('')
    lines.append('| Input | RV nodes | fps achieved | misses | P95 ms | transfer MiB/s | compute headroom | classification |')
    lines.append('|---|---:|---:|---:|---:|---:|---:|---|')
    for r in summary['median_encoded']:
        lines.append(f"| 360p{r['source_fps']} | {r['nodes']} | {fmt(r['throughput_fps'])} | {r['deadline_misses']} | {fmt(r['p95_latency_ms'],1)} | {fmt(r['transfer_mib_s_over_clip'],1)} | {fmt(r['compute_headroom_ratio'])}x | {r['bottleneck']} |")
    lines.append('')
    lines.append('All nine median encoded cases meet the 250 ms offline-buffer deadline in this synthetic model. This is a scheduler/capacity result only; the 30 GOP reconstructed-frame budget is ASSUMED, not measured RV1126 model timing.')
    lines.append('')
    lines.append('## Bandwidth crossover at 40% utilization')
    lines.append('')
    lines.append('Minimum tested full-duplex bandwidth with zero F0 deadline misses:')
    lines.append('')
    lines.append('| Input | nodes | local encoded output | raw 1080p gather |')
    lines.append('|---|---:|---:|---:|')
    for c in summary['crossovers']:
        lines.append(f"| 360p{c['source_fps']} | {c['nodes']} | {c['encoded_crossover_mib_s']} MiB/s | {c['raw_crossover_mib_s']} MiB/s |")
    lines.append('')
    lines.append('Important F0 discovery: lower authoritative fps can be harder on burst latency even when its average input rate is lower. At 5 fps, eleven reconstructed display positions become eligible together when the next authoritative frame arrives. With only a 250 ms playback buffer, this raises the tested encoded-link crossover to 50 MiB/s. At 10 fps the tested grid passes at 12.5 MiB/s. This is a workload-shape effect, not a silicon claim.')
    lines.append('')
    lines.append('A second discovery is that blindly adding frame-parallel nodes can increase context replication. In the 5-fps raw-gather case, 16 nodes require 400 MiB/s on the tested grid while 4/8 nodes cross at 200 MiB/s. F4 must therefore schedule for temporal-context residency, not only available NPU capacity.')
    lines.append('')
    lines.append('## F1 compute envelope')
    lines.append('')
    lines.append('Formula required by the campaign: `2 TOPS × node_count × effective_utilization / reconstructed_positions_per_second`. The table below uses 40% effective utilization and also subtracts the synthetic 12 GOP cost assigned to each genuine source frame.')
    lines.append('')
    lines.append('| source fps | nodes | simple max GOP/recon | after keyframe finish | synthetic need | total synthetic TOPS |')
    lines.append('|---:|---:|---:|---:|---:|---:|')
    for r in summary['f1_40pct']:
        lines.append(f"| {r['source_fps']} | {r['nodes']} | {fmt(r['simple_max_gop_per_reconstructed_frame'],1)} | {fmt(r['max_gop_per_reconstructed_frame_after_keyframe_finish'],1)} | {fmt(r['synthetic_required_gop_per_reconstructed_frame'],1)} | {fmt(r['synthetic_total_required_tops'],3)} |")
    lines.append('')
    lines.append('At 4 nodes, the synthetic workload requires about 1.71 TOPS for 5-fps input, 1.62 TOPS for 10-fps input, and 1.53 TOPS for 15-fps input. Nominal 8 TOPS is therefore not the useful number; at 20% effective utilization the four-chip fabric delivers only 1.6 effective TOPS. The utilization sensitivity sweep correctly fails 5-fps and 10-fps four-node cases at 20% while the 15-fps case passes.')
    lines.append('')
    lines.append('## Bytes')
    lines.append('')
    lines.append(f'- One 360p YUV420 frame: {LOW_FRAME_BYTES} bytes ({LOW_FRAME_BYTES/MIB:.4f} MiB).')
    lines.append(f'- One raw 1080p YUV420 frame: {OUT_FRAME_BYTES} bytes ({OUT_FRAME_BYTES/MIB:.4f} MiB), or {OUT_FRAME_BYTES/MIB*60:.1f} MiB/s at 60 fps before protocol overhead.')
    lines.append(f'- Assumed local encoded output: {ENCODED_BITRATE_MBPS:.0f} Mbit/s = {ENCODED_FRAME_BYTES} bytes/display frame = {ENCODED_FRAME_BYTES/MIB*60:.2f} MiB/s.')
    lines.append(f'- Assumed sidecar: {SIDECAR_BYTES} bytes per cached authoritative generation.')
    lines.append('')
    lines.append('At the 360p10 / 4-node / 40% / 125-MiB/s encoded point, the cache-aware scheduler moves about 292 KiB per displayed frame and 17.1 MiB/s over the ten-second clip. Raw 1080p gathering adds roughly 178 MiB/s and is therefore already beyond a 1-GbE-class payload envelope before practical overhead.')
    lines.append('')
    lines.append('## Facts vs assumptions')
    lines.append('')
    lines.append('**DATASHEET / PRIMARY-SOURCE INPUTS**')
    lines.append('- Original RV1126: quad Cortex-A7 + RISC-V MCU; 2.0 TOPS NPU with INT8/INT16 support; 2D scale up/down; 4K H.264/H.265 encode/decode; RGMII; USB 2.0; dual SDIO 3.0. Source: Rockchip RV1126 product page.')
    lines.append('- Original RV1126 belongs to RKNN-Toolkit/RKNPU, while RK3566/RK3568/RK3588 belong to RKNN-Toolkit2/RKNPU2. Source: airockchip RKNN-Toolkit and RKNN-Toolkit2 repositories.')
    lines.append('- Rockchip MPP lists RV1109/RV1126 among supported hardware platforms. Source: rockchip-linux/mpp.')
    lines.append('')
    lines.append('**ASSUMED FOR F0/F1 ONLY**')
    lines.append('- Delivered NPU throughput is nominal 2 TOPS multiplied by a swept 20–70% effective-utilization factor.')
    lines.append('- 30 GOP per reconstructed display frame: 18 GOP warp/refine + 12 GOP finishing; 12 GOP finishing for genuine source frames.')
    lines.append('- 64 KiB sidecar per authoritative generation; four-generation temporal cache per RV node.')
    lines.append('- 12 Mbit/s local H.265-like encoded output alternative; raw gather alternative is YUV420.')
    lines.append('- 0.6 ms aggregate NPU submission/pre/post overhead per task; 0.08 ms link transaction latency.')
    lines.append('- Full-duplex shared controller link; no packet/protocol loss and no RK3588 saturation yet.')
    lines.append('- 250 ms offline playback buffer. This is not a live-world latency assumption.')
    lines.append('')
    lines.append('**NOT YET CLAIMED**')
    lines.append('- That a 30-GOP interpolation/SR graph exists for RV1126.')
    lines.append('- That its operations are all old-RKNN NPU-native.')
    lines.append('- Silicon memory bandwidth, NPU timing, thermal sustain, Ethernet payload efficiency, MPP multi-stream capacity, visual quality, or GPU savings.')
    lines.append('')
    lines.append('## Exact next experiment — F2')
    lines.append('')
    lines.append('Build an operator-contract matrix from the original RKNN-Toolkit/RKNPU documentation and compile a deliberately tiny RV1126-oriented residual/interpolation graph. Every op is classified NPU_NATIVE / CPU_FALLBACK / REWRITE / PRECOMPUTE_UPSTREAM / DETERMINISTIC_HARDWARE / FATAL. The first executable discriminator is not image quality: it is whether a fixed-shape INT8 graph made only of confirmed old-RKNN-supported primitives converts without hidden CPU fallback. Only after that passes should its measured/estimated GOP count replace the F0 synthetic 30-GOP budget.')
    return '\n'.join(lines) + '\n'

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--outdir', default='.')
    args = ap.parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    f0_bw = []
    for source_fps in SOURCE_FPS_VALUES:
        for nodes in NODE_COUNTS:
            for bandwidth in BW_VALUES:
                for mode in ('encoded', 'raw'):
                    f0_bw.append(simulate(source_fps, nodes, 0.40, bandwidth, mode))
    write_csv(outdir / 'f0_bandwidth_sweep.csv', f0_bw)

    f0_util = []
    for source_fps in SOURCE_FPS_VALUES:
        for nodes in NODE_COUNTS:
            for utilization in UTIL_VALUES:
                f0_util.append(simulate(source_fps, nodes, utilization, 125, 'encoded'))
    write_csv(outdir / 'f0_utilization_sensitivity.csv', f0_util)

    f1 = [f1_envelope(sf, n, u) for sf in SOURCE_FPS_VALUES for n in NODE_COUNTS for u in UTIL_VALUES]
    write_csv(outdir / 'f1_compute_envelope.csv', f1)

    assert all(r['stale_replays_rejected'] == 1 for r in f0_bw + f0_util)
    assert len(f0_bw) == 162 and len(f0_util) == 54 and len(f1) == 54
    median_encoded = [simulate(sf, n, 0.40, 125, 'encoded') for sf in SOURCE_FPS_VALUES for n in NODE_COUNTS]
    assert all(r['deadline_misses'] == 0 for r in median_encoded)

    crossovers = []
    for sf in SOURCE_FPS_VALUES:
        for n in NODE_COUNTS:
            row = {'source_fps': sf, 'nodes': n}
            for mode in ('encoded', 'raw'):
                passing = [r['bandwidth_mib_s'] for r in f0_bw
                           if r['source_fps'] == sf and r['nodes'] == n and r['output_mode'] == mode
                           and r['deadline_misses'] == 0]
                row[f'{mode}_crossover_mib_s'] = min(passing) if passing else None
            crossovers.append(row)

    f1_40 = [r for r in f1 if abs(r['effective_utilization'] - 0.40) < 1e-12]
    summary = {
        'status': 'SOFTWARE_SIMULATION_ONLY',
        'f0_bandwidth_rows': len(f0_bw),
        'f0_utilization_rows': len(f0_util),
        'f1_rows': len(f1),
        'assumptions': {
            'reconstructed_frame_gop': RECON_TOTAL_GOP,
            'authoritative_frame_finish_gop': KEYFRAME_FINISH_GOP,
            'sidecar_bytes_per_authoritative_generation': SIDECAR_BYTES,
            'encoded_bitrate_mbps': ENCODED_BITRATE_MBPS,
            'playback_buffer_ms': PLAYBACK_BUFFER_MS,
            'temporal_cache_generations': CACHE_GENERATIONS,
            'link_latency_ms': LINK_LATENCY_MS,
        },
        'median_encoded': median_encoded,
        'crossovers': crossovers,
        'f1_40pct': f1_40,
    }
    (outdir / 'f0f1_summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    (outdir / 'RV1126_GENERATIVE_VIDEO_FABRIC_F0.md').write_text(build_report(summary))
    print(json.dumps({
        'status': 'F0_F1_PASS',
        'f0_bandwidth_rows': len(f0_bw),
        'f0_utilization_rows': len(f0_util),
        'f1_rows': len(f1),
        'crossovers': crossovers,
    }, indent=2))

if __name__ == '__main__':
    main()
