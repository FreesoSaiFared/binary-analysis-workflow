#!/usr/bin/env python3
"""
RV1126 Generative Video Expansion Fabric — F0/F1 software model.

Conceptual extension of frankenfabric_f02.py:
- explicit ownership + generation + checksum
- stale-generation rejection
- transfer-aware scheduling and contention
- separate node queues and shared interconnect queue

This is a workload/contract model, NOT RV1126 silicon timing emulation.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from hashlib import sha256
import argparse, csv, heapq, json, math
from pathlib import Path

MIB = 1024 * 1024
STATE_BYTES = 8192
OUTPUT_FPS = 60.0
DISPLAY_INTERVAL_MS = 1000.0 / OUTPUT_FPS
NOMINAL_TOPS_PER_RV1126 = 2.0

@dataclass(frozen=True)
class Assumptions:
    keyframe_bytes_360p: int = 345600
    sidecar_bytes_per_authoritative: int = 180000
    dispatch_bytes_per_reconstructed: int = 120000
    encoded_output_bytes_per_frame: int = 20000
    fullres_intermediate_bytes: int = 3110400
    submit_overhead_ms: float = 0.15
    controller_overhead_ms: float = 0.08
    link_latency_ms: float = 0.10
    model_gop_per_reconstructed: float = 40.0
    operator_efficiency: float = 0.85
    quantization_efficiency: float = 0.90
    thermal_factor: float = 0.95

@dataclass
class DecoderState:
    stream: int
    owner: int
    generation: int
    payload: bytes
    checksum: str

@dataclass
class FrameJob:
    display_index: int
    generation: int
    authoritative: bool
    arrival_ms: float
    deadline_ms: float
    owner: int
    state: DecoderState
    start_ms: float = 0.0
    finish_ms: float = 0.0
    network_done_ms: float = 0.0
    node: int = -1
    stale_rejected: bool = False

@dataclass
class Node:
    nid: int
    compute_available_ms: float = 0.0
    busy_ms: float = 0.0
    jobs: int = 0

@dataclass
class Link:
    bandwidth_mib_s: float
    available_ms: float = 0.0
    busy_ms: float = 0.0
    bytes: int = 0

def payload0(stream: int) -> bytes:
    # Compact deterministic token for the logical 8192-byte decoder state.
    # STATE_BYTES remains explicit for migration/accounting; V9 already proved
    # physical 8192-byte copying separately. F0 does not re-prove that copy.
    return sha256(f"rv1126-video-stream={stream}-generation=0".encode()).digest()

def digest(b: bytes) -> str:
    return sha256(b).hexdigest()

def mutate(st: DecoderState, new_owner: int, new_generation: int) -> DecoderState:
    out = sha256(st.payload + new_generation.to_bytes(8, "little") +
                 new_owner.to_bytes(4, "little")).digest()
    return DecoderState(st.stream, new_owner, new_generation, out, digest(out))

def xfer(link: Link, now_ms: float, nbytes: int, latency_ms: float) -> float:
    if nbytes <= 0:
        return now_ms
    start = max(now_ms, link.available_ms)
    dur = latency_ms + (nbytes / MIB) / max(link.bandwidth_mib_s, 1e-9) * 1000.0
    link.available_ms = start + dur
    link.busy_ms += dur
    link.bytes += nbytes
    return link.available_ms

def npu_ms(gop: float, effective_tops_per_node: float, submit_overhead_ms: float) -> float:
    return (gop / effective_tops_per_node) + submit_overhead_ms

def choose_node(nodes: list[Node], ready_ms: float, compute_ms: float) -> Node:
    return min(nodes, key=lambda n: (max(ready_ms, n.compute_available_ms) + compute_ms, n.nid))

def run_case(source_fps: int, node_count: int, effective_utilization: float,
             link_mbps: float, model_gop: float, seconds: float = 10.0,
             assumptions: Assumptions | None = None) -> dict:
    a = assumptions or Assumptions(model_gop_per_reconstructed=model_gop)
    per_node_tops = (NOMINAL_TOPS_PER_RV1126 * effective_utilization *
                     a.operator_efficiency * a.quantization_efficiency * a.thermal_factor)
    nodes = [Node(i) for i in range(node_count)]
    # Full-duplex model, matching frankenfabric_f02's separate ingress/egress clocks.
    ingress_link = Link(link_mbps / 8.0 / 1.048576)
    egress_link = Link(link_mbps / 8.0 / 1.048576)

    p = payload0(0)
    state = DecoderState(0, 0, 0, p, digest(p))
    stale_saved = None
    stale_rejects = 0
    duplicate_authority = 0
    jobs = []
    source_period = OUTPUT_FPS / source_fps
    total_frames = int(round(seconds * OUTPUT_FPS))

    source_network_bytes = 0
    recon_network_bytes = 0
    output_network_bytes = 0

    for idx in range(total_frames):
        t_display = idx * DISPLAY_INTERVAL_MS
        auth = (idx % round(source_period) == 0)
        generation = idx
        if generation <= state.generation and idx != 0:
            duplicate_authority += 1
            raise RuntimeError("non-monotonic generation")
        if idx == 0:
            generation = 0

        if auth:
            ingress = a.keyframe_bytes_360p + a.sidecar_bytes_per_authoritative
            ready = xfer(ingress_link, t_display, ingress, a.link_latency_ms)
            source_network_bytes += ingress
            compute_ms = a.controller_overhead_ms
        else:
            ingress = a.dispatch_bytes_per_reconstructed
            ready = xfer(ingress_link, t_display, ingress, a.link_latency_ms)
            source_network_bytes += ingress
            recon_network_bytes += ingress
            compute_ms = npu_ms(model_gop, per_node_tops, a.submit_overhead_ms)

        node = choose_node(nodes, ready, compute_ms)
        start = max(ready, node.compute_available_ms)
        finish = start + compute_ms
        node.compute_available_ms = finish
        node.busy_ms += compute_ms
        node.jobs += 1

        if idx == 1:
            stale_saved = DecoderState(state.stream, state.owner, state.generation,
                                       bytes(state.payload), state.checksum)
        state = mutate(state, node.nid, generation)

        stale_rejected = False
        if idx == 3 and stale_saved is not None:
            if stale_saved.generation < state.generation:
                stale_rejects += 1
                stale_rejected = True
            else:
                raise RuntimeError("stale generation was not stale")

        out_done = xfer(egress_link, finish, a.encoded_output_bytes_per_frame, a.link_latency_ms)
        output_network_bytes += a.encoded_output_bytes_per_frame

        jobs.append(FrameJob(
            display_index=idx, generation=generation, authoritative=auth,
            arrival_ms=t_display, deadline_ms=t_display + DISPLAY_INTERVAL_MS,
            owner=node.nid, state=state, start_ms=start, finish_ms=out_done,
            network_done_ms=out_done, node=node.nid, stale_rejected=stale_rejected
        ))

    latencies = [j.finish_ms - j.arrival_ms for j in jobs]
    misses = [j for j in jobs if j.finish_ms > j.deadline_ms]
    makespan = max(j.finish_ms for j in jobs) if jobs else 0.0
    achieved_fps = total_frames / (makespan / 1000.0) if makespan else 0.0
    link_capacity_bytes_s = ingress_link.bandwidth_mib_s * MIB
    ingress_bytes_s = ingress_link.bytes / seconds
    egress_bytes_s = egress_link.bytes / seconds
    avg_bytes_s = ingress_bytes_s + egress_bytes_s
    link_load = max(ingress_bytes_s, egress_bytes_s) / link_capacity_bytes_s
    compute_capacity_tops = per_node_tops * node_count
    reconstructed_fps = OUTPUT_FPS - source_fps
    required_tops = reconstructed_fps * model_gop / 1000.0
    compute_load = required_tops / compute_capacity_tops if compute_capacity_tops else math.inf

    if compute_load >= 1.0 and link_load >= 1.0:
        bottleneck = "compute+link"
    elif compute_load >= 1.0:
        bottleneck = "compute"
    elif link_load >= 1.0:
        bottleneck = "link"
    elif len(misses) > 0 and max(n.compute_available_ms for n in nodes) > max(ingress_link.available_ms, egress_link.available_ms):
        bottleneck = "compute_queue/deadline"
    elif len(misses) > 0:
        bottleneck = "link_queue/deadline"
    else:
        bottleneck = "headroom"

    slat = sorted(latencies)
    def pct(q):
        if not slat: return 0.0
        pos = (len(slat)-1)*q
        lo = int(math.floor(pos)); hi = int(math.ceil(pos))
        if lo == hi: return slat[lo]
        return slat[lo] + (slat[hi]-slat[lo])*(pos-lo)

    return {
        "source_fps": source_fps,
        "output_fps_target": OUTPUT_FPS,
        "reconstructed_fps": reconstructed_fps,
        "node_count": node_count,
        "nominal_tops_total": NOMINAL_TOPS_PER_RV1126 * node_count,
        "effective_utilization": effective_utilization,
        "operator_efficiency": a.operator_efficiency,
        "quantization_efficiency": a.quantization_efficiency,
        "thermal_factor": a.thermal_factor,
        "effective_tops_total": round(compute_capacity_tops, 6),
        "model_gop_per_reconstructed": model_gop,
        "required_tops": round(required_tops, 6),
        "compute_load": round(compute_load, 6),
        "link_mbps": link_mbps,
        "link_bytes_total": ingress_link.bytes + egress_link.bytes,
        "ingress_bytes_total": ingress_link.bytes,
        "egress_bytes_total": egress_link.bytes,
        "source_network_bytes": source_network_bytes,
        "recon_dispatch_bytes": recon_network_bytes,
        "output_network_bytes": output_network_bytes,
        "bytes_per_output_frame_avg": round((ingress_link.bytes + egress_link.bytes) / total_frames, 3),
        "bytes_per_second_avg": round(avg_bytes_s, 3),
        "link_load": round(link_load, 6),
        "steady_state_fps": round(achieved_fps, 3),
        "p50_latency_ms": round(pct(.50), 3),
        "p95_latency_ms": round(pct(.95), 3),
        "p99_latency_ms": round(pct(.99), 3),
        "deadline_misses": len(misses),
        "deadline_miss_rate": round(len(misses)/total_frames, 6),
        "stale_generations_rejected": stale_rejects,
        "duplicate_authoritative_owner_errors": duplicate_authority,
        "final_generation": state.generation,
        "final_owner": state.owner,
        "final_checksum": state.checksum,
        "bottleneck": bottleneck,
        "node_jobs": {str(n.nid): n.jobs for n in nodes},
        "node_busy_ms": {str(n.nid): round(n.busy_ms, 3) for n in nodes},
    }

def f1_budget(source_fps: int, node_count: int, util: float,
              operator_eff=0.85, quant_eff=0.90, thermal=0.95) -> dict:
    reconstructed = OUTPUT_FPS - source_fps
    delivered_tops = NOMINAL_TOPS_PER_RV1126 * node_count * util * operator_eff * quant_eff * thermal
    max_gop = delivered_tops * 1000.0 / reconstructed
    util_only_tops = NOMINAL_TOPS_PER_RV1126 * node_count * util
    util_only_max_gop = util_only_tops * 1000.0 / reconstructed
    return {
        "source_fps": source_fps, "node_count": node_count, "effective_utilization": util,
        "reconstructed_fps": reconstructed,
        "util_only_effective_tops": util_only_tops,
        "util_only_max_gop_per_reconstructed_frame": util_only_max_gop,
        "derated_effective_tops": delivered_tops,
        "derated_max_gop_per_reconstructed_frame": max_gop,
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".")
    ap.add_argument("--seconds", type=float, default=10.0)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    source_fps_values = (5,10,15)
    nodes_values = (4,8,16)
    util_values = (0.20,0.30,0.40,0.50,0.60,0.70)
    link_values = (100,250,500,1000,2500,5000)
    gop_values = (10,20,40,80,120,160)

    rows=[]
    for sf in source_fps_values:
        for nc in nodes_values:
            for util in util_values:
                for link in link_values:
                    for gop in gop_values:
                        rows.append(run_case(sf,nc,util,link,gop,args.seconds))

    with open(out/"rv1126_video_fabric_f0_sweep.json","w") as f:
        json.dump(rows,f,indent=2)
    with open(out/"rv1126_video_fabric_f0_sweep.csv","w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]))
        w.writeheader()
        for r in rows:
            rr=dict(r)
            for k in ("node_jobs","node_busy_ms"):
                rr[k]=json.dumps(rr[k],sort_keys=True)
            w.writerow(rr)

    budgets=[f1_budget(sf,nc,u) for sf in source_fps_values for nc in nodes_values for u in util_values]
    with open(out/"rv1126_video_fabric_f1_budget.json","w") as f:
        json.dump(budgets,f,indent=2)
    with open(out/"rv1126_video_fabric_f1_budget.csv","w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(budgets[0]))
        w.writeheader(); w.writerows(budgets)

    compact=[r for r in rows if r["effective_utilization"]==0.40 and r["link_mbps"]==1000 and r["model_gop_per_reconstructed"]==40]
    with open(out/"rv1126_video_fabric_f0_compact.json","w") as f:
        json.dump(compact,f,indent=2)
    print(json.dumps({"cases":len(rows),"budget_cases":len(budgets),"compact":compact},indent=2))

if __name__=="__main__":
    main()
