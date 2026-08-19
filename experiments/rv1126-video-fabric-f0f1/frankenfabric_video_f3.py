#!/usr/bin/env python3
from __future__ import annotations
from dataclasses import dataclass, field
from collections import OrderedDict
from pathlib import Path
import argparse,csv,json,math,statistics

MIB=1024*1024
OUT_FPS=60
DURATION_S=10
OUTPUT_FRAMES=600
YUV420_BPP=1.5
OUT_W,OUT_H=1920,1080
INTERMEDIATE_W,INTERMEDIATE_H=1280,720
OUT_RAW_BYTES=int(OUT_W*OUT_H*YUV420_BPP)
INTERMEDIATE_RAW_BYTES=int(INTERMEDIATE_W*INTERMEDIATE_H*YUV420_BPP)
SIDECAR_BYTES=64*1024
BASE_ENCODED_MBPS=12.0
NOMINAL_TOPS_PER_NODE=2.0
OPS_PER_PIXEL=1248  # exact F2 topology, MAC counted as 2 operations
SUBMIT_MS=0.25
PREPOST_MS=0.35
LINK_TXN_MS=0.08
CACHE_GENERATIONS=4
OFFLINE_BUFFER_MS=250.0

SOURCE_MODES={
    "360p":(640,360),
    "540p":(960,540),
    "720p":(1280,720),
}
SOURCE_FPS=(5,10,15)
NODE_COUNTS=(4,8,16)
BW_VALUES=(12.5,25,50,100,125,200,400,800,1600)
UTIL_VALUES=(.20,.30,.40,.50,.60,.70)
TRANSPORTS=("owner_only","finishing_owner","independent_au","raw_gather")

# None of these profiles is a quality claim. They are workload fractions only.
SCENES={
 "optimistic":{"base_refine":.15,"disocc":.02,"sr_refine":.25,"source_passes":1,"sr_passes":1,
               "warp_ms_per_mp":.8,"scale_ms_per_mp":.35,"au_penalty":1.35},
 "median":{"base_refine":.35,"disocc":.08,"sr_refine":.50,"source_passes":2,"sr_passes":2,
           "warp_ms_per_mp":1.5,"scale_ms_per_mp":.65,"au_penalty":1.8},
 "adversarial":{"base_refine":.70,"disocc":.20,"sr_refine":.85,"source_passes":4,"sr_passes":4,
                "warp_ms_per_mp":3.0,"scale_ms_per_mp":1.2,"au_penalty":3.0},
}

@dataclass
class Node:
    node_id:int
    available_ms:float=0.0
    busy_ms:float=0.0
    cache:OrderedDict[int,bool]=field(default_factory=OrderedDict)
    def has_all(self, gens): return all(g in self.cache for g in gens)
    def touch(self, gen:int):
        if gen in self.cache:self.cache.move_to_end(gen)
        else:
            self.cache[gen]=True
            while len(self.cache)>CACHE_GENERATIONS:self.cache.popitem(last=False)

@dataclass
class GenerationState:
    authoritative_generation:int=-1
    stale_rejected:int=0
    def accept(self,g:int)->bool:
        if g<=self.authoritative_generation:
            self.stale_rejected+=1
            return False
        self.authoritative_generation=g
        return True

def frame_bytes(w,h): return int(w*h*YUV420_BPP)
def graph_gop(w,h): return w*h*OPS_PER_PIXEL/1e9

def component_budget(source_res:str, scene_name:str, authoritative:bool)->dict:
    sw,sh=SOURCE_MODES[source_res]
    s=SCENES[scene_name]
    source_full=graph_gop(sw,sh)
    sr_full=graph_gop(INTERMEDIATE_W,INTERMEDIATE_H)
    if authoritative:
        temporal_gop=0.0
        disocc_gop=0.0
        sr_gop=sr_full*s["sr_refine"]*s["sr_passes"]
        warp_ms=0.0
    else:
        temporal_frac=min(1.0,s["base_refine"]+s["disocc"])
        temporal_gop=source_full*temporal_frac*s["source_passes"]
        disocc_gop=source_full*s["disocc"]*s["source_passes"]
        sr_gop=sr_full*s["sr_refine"]*s["sr_passes"]
        warp_ms=s["warp_ms_per_mp"]*(sw*sh/1e6)
    scale_ms=s["scale_ms_per_mp"]*(INTERMEDIATE_W*INTERMEDIATE_H/1e6)
    return {
      "temporal_residual_gop":temporal_gop,
      "disocclusion_extra_gop":disocc_gop,
      "sr_residual_gop":sr_gop,
      "total_gop":temporal_gop+disocc_gop+sr_gop,
      "deterministic_warp_ms":warp_ms,
      "deterministic_scale_ms":scale_ms,
    }

def task_for(frame_index:int, source_fps:int, source_res:str, scene:str)->dict:
    stride=OUT_FPS//source_fps
    auth_count=source_fps*DURATION_S
    is_auth=(frame_index%stride)==0
    prev_gen=min(frame_index//stride,auth_count-1)
    next_gen=min(prev_gen+1,auth_count-1)
    if is_auth:
        contexts=(prev_gen,)
        release_ms=prev_gen*1000/source_fps
    else:
        contexts=tuple(dict.fromkeys((prev_gen,next_gen)))
        release_ms=next_gen*1000/source_fps
    display_ms=frame_index*1000/OUT_FPS
    b=component_budget(source_res,scene,is_auth)
    return {
      "frame":frame_index,"authoritative":is_auth,"contexts":contexts,
      "release_ms":release_ms,"deadline_ms":display_ms+OFFLINE_BUFFER_MS,
      **b
    }

def percentile(v,p):
    x=sorted(v); return x[min(len(x)-1,max(0,math.ceil(len(x)*p)-1))]

def classify(r):
    if r["deadline_misses"]>0 and r["compute_capacity_ratio"]<1:return "COMPUTE_CAPACITY"
    if r["deadline_misses"]>0 and r["link_busy_fraction"]>.80:return "INTERCONNECT"
    if r["deadline_misses"]>0:return "BURST_OR_PER_FRAME_LATENCY"
    if r["link_busy_fraction"]>.70:return "INTERCONNECT_HEADROOM_LOW"
    if r["compute_capacity_ratio"]<1.25:return "COMPUTE_HEADROOM_LOW"
    if r["max_node_busy_fraction"]>.70:return "NODE_QUEUE_HEADROOM_LOW"
    return "HEADROOM"

def simulate(source_res:str,source_fps:int,nodes_n:int,scene:str,transport:str,util:float,bw_mib_s:float)->dict:
    sw,sh=SOURCE_MODES[source_res]
    source_context_bytes=frame_bytes(sw,sh)+SIDECAR_BYTES
    nodes=[Node(i) for i in range(nodes_n)]
    delivered_tops=NOMINAL_TOPS_PER_NODE*util

    # F3 preserves F0's full-duplex assumption: context ingress and reconstructed
    # output/peer traffic have independent direction queues. A shared/half-duplex
    # switch is an explicit later adverse topology, not silently conflated here.
    ingress_available=0.0
    ingress_busy=0.0
    egress_available=0.0
    egress_busy=0.0
    total_context_bytes=0
    total_output_bytes=0
    output_requests=[]
    tasks=sorted((task_for(i,source_fps,source_res,scene) for i in range(OUTPUT_FRAMES)),
                 key=lambda t:(t["release_ms"],t["frame"]))

    total_required_gop=sum(t["total_gop"] for t in tasks)
    for t in tasks:
        candidate_nodes=nodes[:1] if transport=="owner_only" else nodes
        best=None
        for node in candidate_nodes:
            missing=[g for g in t["contexts"] if g not in node.cache]
            in_bytes=len(missing)*source_context_bytes
            xfer_start=max(t["release_ms"],ingress_available)
            xfer_ms=(LINK_TXN_MS+in_bytes/MIB/bw_mib_s*1000) if in_bytes else 0.0
            xfer_done=xfer_start+xfer_ms
            start=max(node.available_ms,xfer_done)
            neural_ms=t["total_gop"]/delivered_tops
            duration=neural_ms+t["deterministic_warp_ms"]+t["deterministic_scale_ms"]+SUBMIT_MS+PREPOST_MS
            done=start+duration
            cand=(done,len(missing),node.node_id,in_bytes,xfer_start,xfer_ms,duration)
            if best is None or cand<best: best=cand

        done,_,nid,in_bytes,xfer_start,xfer_ms,duration=best
        node=nodes[nid]
        if in_bytes:
            ingress_available=xfer_start+xfer_ms
            ingress_busy+=xfer_ms
            total_context_bytes+=in_bytes
        for g in t["contexts"]:node.touch(g)
        node.available_ms=done
        node.busy_ms+=duration

        if transport=="raw_gather":
            out_bytes=OUT_RAW_BYTES
        elif transport=="independent_au":
            penalty=SCENES[scene]["au_penalty"]
            out_bytes=int(BASE_ENCODED_MBPS*penalty*1e6/8/OUT_FPS)
        elif transport=="finishing_owner":
            intermediate=0 if nid==0 else INTERMEDIATE_RAW_BYTES
            encoded=int(BASE_ENCODED_MBPS*1e6/8/OUT_FPS)
            out_bytes=intermediate+encoded
        elif transport=="owner_only":
            out_bytes=int(BASE_ENCODED_MBPS*1e6/8/OUT_FPS)
        else: raise ValueError(transport)
        output_requests.append((done,t,out_bytes))

    completed=[]
    for ready,t,out_bytes in sorted(output_requests,key=lambda x:(x[0],x[1]["frame"])):
        start=max(ready,egress_available)
        xfer_ms=LINK_TXN_MS+out_bytes/MIB/bw_mib_s*1000
        done=start+xfer_ms
        egress_available=done
        egress_busy+=xfer_ms
        total_output_bytes+=out_bytes
        completed.append((t,done))

    lat=[done-t["release_ms"] for t,done in completed]
    misses=sum(done>t["deadline_ms"]+1e-9 for t,done in completed)
    final_done=max(done for _,done in completed)
    first_release=min(t["release_ms"] for t,_ in completed)
    wall_s=(final_done-first_release)/1000

    gs=GenerationState()
    assert gs.accept(104) and gs.accept(105) and not gs.accept(104) and gs.stale_rejected==1

    available_gop_s=2000*nodes_n*util if transport!="owner_only" else 2000*util
    required_gop_s=total_required_gop/DURATION_S
    result={
      "source_res":source_res,"source_fps":source_fps,"nodes":nodes_n,"scene":scene,
      "transport":transport,"effective_utilization":util,"bandwidth_mib_s":bw_mib_s,
      "throughput_fps":OUTPUT_FRAMES/wall_s,
      "p50_latency_ms":statistics.median(lat),"p95_latency_ms":percentile(lat,.95),"p99_latency_ms":percentile(lat,.99),
      "deadline_misses":misses,
      "context_bytes":total_context_bytes,"output_transport_bytes":total_output_bytes,
      "total_transfer_bytes":total_context_bytes+total_output_bytes,
      "avg_bytes_per_display_frame":(total_context_bytes+total_output_bytes)/OUTPUT_FRAMES,
      "avg_mib_s_over_clip":(total_context_bytes+total_output_bytes)/MIB/DURATION_S,
      "ingress_busy_fraction":ingress_busy/final_done,
      "egress_busy_fraction":egress_busy/final_done,
      "link_busy_fraction":max(ingress_busy,egress_busy)/final_done,
      "max_node_busy_fraction":max(n.busy_ms/final_done for n in nodes),
      "required_effective_tops":required_gop_s/1000,
      "available_effective_tops":available_gop_s/1000,
      "compute_capacity_ratio":available_gop_s/required_gop_s if required_gop_s else math.inf,
      "reconstructed_gop":component_budget(source_res,scene,False)["total_gop"],
      "authoritative_gop":component_budget(source_res,scene,True)["total_gop"],
      "stale_replays_rejected":gs.stale_rejected,
    }
    result["bottleneck"]=classify(result)
    return result

def write_csv(path,rows):
    with open(path,"w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--outdir",default=".");args=ap.parse_args()
    out=Path(args.outdir);out.mkdir(parents=True,exist_ok=True)
    rows=[]
    for sr in SOURCE_MODES:
      for sf in SOURCE_FPS:
       for n in NODE_COUNTS:
        for scene in SCENES:
         for tr in TRANSPORTS:
          for bw in BW_VALUES:
           rows.append(simulate(sr,sf,n,scene,tr,.40,bw))
    write_csv(out/"f3_transport_bandwidth_sweep.csv",rows)

    util_rows=[]
    for sr in SOURCE_MODES:
      for sf in SOURCE_FPS:
       for n in NODE_COUNTS:
        for scene in SCENES:
         for tr in TRANSPORTS:
          for u in UTIL_VALUES:
           util_rows.append(simulate(sr,sf,n,scene,tr,u,125))
    write_csv(out/"f3_utilization_sweep.csv",util_rows)

    comps=[]
    for sr in SOURCE_MODES:
      for scene in SCENES:
       for auth in (False,True):
        b=component_budget(sr,scene,auth)
        comps.append({"source_res":sr,"scene":scene,"authoritative":auth,**b})
    write_csv(out/"f3_component_budget.csv",comps)

    cross=[]
    for sr in SOURCE_MODES:
      for sf in SOURCE_FPS:
       for n in NODE_COUNTS:
        for scene in SCENES:
         rec={"source_res":sr,"source_fps":sf,"nodes":n,"scene":scene}
         for tr in TRANSPORTS:
          ok=[r["bandwidth_mib_s"] for r in rows if r["source_res"]==sr and r["source_fps"]==sf and r["nodes"]==n and r["scene"]==scene and r["transport"]==tr and r["deadline_misses"]==0]
          rec[tr+"_crossover_mib_s"]=min(ok) if ok else None
         cross.append(rec)
    write_csv(out/"f3_crossovers.csv",cross)

    median_focus=[simulate("360p",sf,4,"median",tr,.40,125) for sf in SOURCE_FPS for tr in TRANSPORTS]
    assert all(r["stale_replays_rejected"]==1 for r in rows+util_rows)
    assert component_budget("360p","median",False)["total_gop"] < 5.0
    summary={
      "status":"F3_COMPONENT_SOFTWARE_SIMULATION_ONLY",
      "rows_bandwidth":len(rows),"rows_utilization":len(util_rows),
      "ops_per_pixel_f2_graph":OPS_PER_PIXEL,
      "median_360p_4node_125mib":median_focus,
      "crossovers_360p_median_4node":[r for r in cross if r["source_res"]=="360p" and r["nodes"]==4 and r["scene"]=="median"],
      "component_budgets":[r for r in comps if r["scene"]=="median"],
      "limitations":[
        "No visual-quality result; fractions/passes are workload scenarios.",
        "No RV1126 silicon timing or memory-bandwidth proof.",
        "Finishing-owner 720p intermediate and independent-AU bitrate penalties are assumed transport models.",
        "F3 preserves F0 full-duplex ingress/egress queues; shared/half-duplex switch contention is deferred to adverse topology sweeps.",
        "MPP encode capacity and cross-node bitstream assembly remain unmeasured."
      ]
    }
    (out/"f3_summary.json").write_text(json.dumps(summary,indent=2)+"\n")
    print(json.dumps({
      "status":"F3_PASS",
      "rows_bandwidth":len(rows),"rows_utilization":len(util_rows),
      "median_recon_gop":component_budget("360p","median",False)["total_gop"],
      "median_auth_gop":component_budget("360p","median",True)["total_gop"],
      "focus":[{k:r[k] for k in ("source_fps","transport","throughput_fps","deadline_misses","avg_mib_s_over_clip","compute_capacity_ratio","bottleneck")} for r in median_focus],
      "crossovers":summary["crossovers_360p_median_4node"]
    },indent=2))
if __name__=="__main__":main()
