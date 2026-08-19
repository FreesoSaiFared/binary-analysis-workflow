#!/usr/bin/env python3
from __future__ import annotations
from dataclasses import dataclass, field
from collections import OrderedDict
from pathlib import Path
import argparse,csv,json,math,statistics

MIB=1024*1024
NODES=4
OUT_FPS=60
SOURCE_FPS=10
DURATION_S=10
FRAMES_PER_STREAM=OUT_FPS*DURATION_S
AUTH_INTERVAL_MS=1000/SOURCE_FPS
OFFLINE_BUFFER_MS=250.0
UTIL_DEFAULT=.40
BW_DEFAULT=125.0
TOPS_PER_NODE=2.0
DELIVERED_TOPS=lambda u: TOPS_PER_NODE*u
LOW_W,LOW_H=640,360
MID_W,MID_H=1280,720
BPP=1.5
LOW_BYTES=int(LOW_W*LOW_H*BPP)
MID_BYTES=int(MID_W*MID_H*BPP)
SIDECAR=64*1024
ENCODED_FRAME=int(12e6/8/OUT_FPS)
AU_PENALTY=1.8
CACHE_GENS=4
TXN_MS=.08
OVERHEAD_MS=.60
WARP_MS=1.5*(LOW_W*LOW_H/1e6)
SCALE_MS=.65*(MID_W*MID_H/1e6)
RECON_TEMP_GOP=.293289984
RECON_SR_GOP=1.1501568
RECON_GOP=RECON_TEMP_GOP+RECON_SR_GOP
AUTH_GOP=RECON_SR_GOP
HYST_MS=3.0
HALO=16
TILE_SEAM_GOP=.06
STREAM_COUNTS=(1,2,4,8,12,16,20,24,28,32,40,48)
POLICIES=('stream_owner','frame_parallel','adaptive_v9','stage_pipeline','tile_parallel')

@dataclass
class Node:
    id:int
    available:float=0.0
    busy:float=0.0
    cache:OrderedDict=field(default_factory=OrderedDict)
    def has(self,key): return key in self.cache
    def touch(self,key):
        if key in self.cache:self.cache.move_to_end(key)
        else:
            self.cache[key]=1
            while len(self.cache)>CACHE_GENS*64:
                self.cache.popitem(last=False)

@dataclass
class GenState:
    generation:int=-1
    stale:int=0
    def accept(self,g):
        if g<=self.generation:
            self.stale+=1; return False
        self.generation=g; return True

def pct(v,p):
    x=sorted(v); return x[min(len(x)-1,max(0,math.ceil(len(x)*p)-1))]

def tasks(streams:int,phase_mode:str):
    all_tasks=[]
    stride=OUT_FPS//SOURCE_FPS
    auth_count=SOURCE_FPS*DURATION_S
    for s in range(streams):
        phase=0.0 if phase_mode=='synchronized' else (s/streams)*AUTH_INTERVAL_MS
        for f in range(FRAMES_PER_STREAM):
            auth=(f%stride)==0
            prev=min(f//stride,auth_count-1)
            nxt=min(prev+1,auth_count-1)
            if auth:
                contexts=(prev,)
                release=phase+prev*AUTH_INTERVAL_MS
                gop=AUTH_GOP
                warp=0.0
            else:
                contexts=tuple(dict.fromkeys((prev,nxt)))
                release=phase+nxt*AUTH_INTERVAL_MS
                gop=RECON_GOP
                warp=WARP_MS
            display=phase+f*1000/OUT_FPS
            all_tasks.append({
                'stream':s,'frame':f,'auth':auth,'contexts':contexts,
                'release':release,'deadline':display+OFFLINE_BUFFER_MS,
                'gop':gop,'warp':warp
            })
    all_tasks.sort(key=lambda t:(t['release'],t['stream'],t['frame']))
    return all_tasks

def xfer(start_ready, bytes_n, available, bw):
    if bytes_n<=0:return max(start_ready,available),0.0,max(start_ready,available)
    start=max(start_ready,available)
    dur=TXN_MS+bytes_n/MIB/bw*1000
    return start,dur,start+dur

def node_duration(t,u,gop=None, extra_det=0.0):
    g=t['gop'] if gop is None else gop
    return g/DELIVERED_TOPS(u)+t['warp']+SCALE_MS+OVERHEAD_MS+extra_det

def simulate_simple(streams,policy,u,bw,phase_mode):
    ns=[Node(i) for i in range(NODES)]
    ingress_av=egress_av=0.0
    ingress_busy=egress_busy=0.0
    in_bytes=out_bytes=0
    completes=[]
    offloads=0
    context_misses=0
    migration_control_bytes=0
    gs={s:GenState() for s in range(streams)}
    for s,g in gs.items():
        assert g.accept(104) and g.accept(105) and not g.accept(104) and g.stale==1

    for t in tasks(streams,phase_mode):
        s=t['stream']; owner=s%NODES
        context_keys=[(s,g) for g in t['contexts']]
        if policy=='stream_owner':
            candidates=[owner]
        else:
            candidates=list(range(NODES))

        best=None
        for nid in candidates:
            n=ns[nid]
            missing=[k for k in context_keys if not n.has(k)]
            b=len(missing)*(LOW_BYTES+SIDECAR)
            xs,xd,xe=xfer(t['release'],b,ingress_av,bw)
            start=max(xe,n.available)
            dur=node_duration(t,u)
            done=start+dur
            score=done
            if policy=='adaptive_v9':
                predicted_out = ENCODED_FRAME if nid==owner else int(ENCODED_FRAME*AU_PENALTY)
                _,_,predicted_finish = xfer(done,predicted_out,egress_av,bw)
                score = predicted_finish + (HYST_MS if nid!=owner else 0.0)
            cand=(score,done,len(missing),nid,b,xs,xd,dur)
            if best is None or cand<best:best=cand

        _,done,misses,nid,b,xs,xd,dur=best
        n=ns[nid]
        if b:
            ingress_av=xs+xd;ingress_busy+=xd;in_bytes+=b;context_misses+=misses
        for k in context_keys:n.touch(k)
        n.available=done;n.busy+=dur
        if nid!=owner:
            offloads+=1
            migration_control_bytes+=4096

        if policy=='frame_parallel':
            ob=int(ENCODED_FRAME*AU_PENALTY)
        elif policy=='adaptive_v9' and nid!=owner:
            ob=int(ENCODED_FRAME*AU_PENALTY)
        else:
            ob=ENCODED_FRAME
        os,od,oe=xfer(done,ob,egress_av,bw)
        egress_av=oe;egress_busy+=od;out_bytes+=ob
        completes.append((t,oe))

    lats=[done-t['release'] for t,done in completes]
    misses=sum(done>t['deadline']+1e-9 for t,done in completes)
    final=max(done for _,done in completes)
    total_frames=streams*FRAMES_PER_STREAM
    return {
      'streams':streams,'policy':policy,'phase_mode':phase_mode,'util':u,'bandwidth_mib_s':bw,
      'throughput_fps_total':total_frames/(final/1000),
      'throughput_fps_per_stream':total_frames/(final/1000)/streams,
      'deadline_misses':misses,'miss_rate':misses/total_frames,
      'p50_latency_ms':statistics.median(lats),'p95_latency_ms':pct(lats,.95),'p99_latency_ms':pct(lats,.99),
      'input_mib_s':in_bytes/MIB/DURATION_S,'output_mib_s':out_bytes/MIB/DURATION_S,
      'total_mib_s':(in_bytes+out_bytes)/MIB/DURATION_S,
      'context_misses':context_misses,'offloaded_frames':offloads,
      'migration_control_bytes':migration_control_bytes,
      'max_node_busy_fraction':max(n.busy/final for n in ns),
      'ingress_busy_fraction':ingress_busy/final,'egress_busy_fraction':egress_busy/final,
      'stale_rejected':sum(g.stale for g in gs.values())
    }

def simulate_stage(streams,u,bw,phase_mode):
    ns=[Node(i) for i in range(NODES)]
    ingress_av=peer_av=egress_av=0.0
    ingress_busy=peer_busy=egress_busy=0.0
    in_bytes=peer_bytes=out_bytes=0
    completes=[]
    for t in tasks(streams,phase_mode):
        lane=t['stream']%2
        n0=ns[lane*2];n1=ns[lane*2+1]
        keys=[(t['stream'],g) for g in t['contexts']]
        missing=[k for k in keys if not n0.has(k)]
        b=len(missing)*(LOW_BYTES+SIDECAR)
        xs,xd,xe=xfer(t['release'],b,ingress_av,bw)
        if b:ingress_av=xe;ingress_busy+=xd;in_bytes+=b
        for k in keys:n0.touch(k)
        g1=0.0 if t['auth'] else RECON_TEMP_GOP
        d1=g1/DELIVERED_TOPS(u)+(0 if t['auth'] else WARP_MS)+SCALE_MS+OVERHEAD_MS
        s1=max(xe,n0.available);e1=s1+d1;n0.available=e1;n0.busy+=d1
        ps,pd,pe=xfer(e1,MID_BYTES,peer_av,bw)
        peer_av=pe;peer_busy+=pd;peer_bytes+=MID_BYTES
        d2=RECON_SR_GOP/DELIVERED_TOPS(u)+OVERHEAD_MS
        s2=max(pe,n1.available);e2=s2+d2;n1.available=e2;n1.busy+=d2
        os,od,oe=xfer(e2,ENCODED_FRAME,egress_av,bw)
        egress_av=oe;egress_busy+=od;out_bytes+=ENCODED_FRAME
        completes.append((t,oe))
    return finalize(streams,'stage_pipeline',phase_mode,u,bw,ns,completes,in_bytes,out_bytes,peer_bytes,
                    ingress_busy,egress_busy,peer_busy,0,0,0,streams)

def tile_area_factor():
    base=LOW_W*LOW_H
    total=0
    tile=LOW_W//4
    for i in range(4):
        left=0 if i==0 else HALO
        right=0 if i==3 else HALO
        total+=(tile+left+right)*LOW_H
    return total/base

def simulate_tile(streams,u,bw,phase_mode):
    ns=[Node(i) for i in range(NODES)]
    ingress_av=peer_av=egress_av=0.0
    ingress_busy=peer_busy=egress_busy=0.0
    in_bytes=peer_bytes=out_bytes=0
    completes=[]
    fac=tile_area_factor()
    for t in tasks(streams,phase_mode):
        tile_ends=[]
        for i,n in enumerate(ns):
            ctx_count=len(t['contexts'])
            b=int(ctx_count*((LOW_BYTES*fac/4)+(SIDECAR/4)))
            xs,xd,xe=xfer(t['release'],b,ingress_av,bw)
            ingress_av=xe;ingress_busy+=xd;in_bytes+=b
            g=t['gop']*fac/4
            dur=g/DELIVERED_TOPS(u)+t['warp']+SCALE_MS+OVERHEAD_MS
            st=max(xe,n.available);en=st+dur;n.available=en;n.busy+=dur
            tile_ends.append(en)
        ready=max(tile_ends)
        gather=int(MID_BYTES*fac*3/4)
        ps,pd,pe=xfer(ready,gather,peer_av,bw)
        peer_av=pe;peer_busy+=pd;peer_bytes+=gather
        owner=ns[t['stream']%NODES]
        seam=TILE_SEAM_GOP/DELIVERED_TOPS(u)+OVERHEAD_MS
        ss=max(pe,owner.available);se=ss+seam;owner.available=se;owner.busy+=seam
        os,od,oe=xfer(se,ENCODED_FRAME,egress_av,bw)
        egress_av=oe;egress_busy+=od;out_bytes+=ENCODED_FRAME
        completes.append((t,oe))
    return finalize(streams,'tile_parallel',phase_mode,u,bw,ns,completes,in_bytes,out_bytes,peer_bytes,
                    ingress_busy,egress_busy,peer_busy,0,0,0,streams)

def finalize(streams,policy,phase,u,bw,ns,completes,in_bytes,out_bytes,peer_bytes,
             ib,ob,pb,ctxmiss,offloads,mig,stale):
    lats=[done-t['release'] for t,done in completes]
    misses=sum(done>t['deadline']+1e-9 for t,done in completes)
    final=max(done for _,done in completes)
    total=streams*FRAMES_PER_STREAM
    return {
      'streams':streams,'policy':policy,'phase_mode':phase,'util':u,'bandwidth_mib_s':bw,
      'throughput_fps_total':total/(final/1000),'throughput_fps_per_stream':total/(final/1000)/streams,
      'deadline_misses':misses,'miss_rate':misses/total,
      'p50_latency_ms':statistics.median(lats),'p95_latency_ms':pct(lats,.95),'p99_latency_ms':pct(lats,.99),
      'input_mib_s':in_bytes/MIB/DURATION_S,'output_mib_s':out_bytes/MIB/DURATION_S,
      'peer_mib_s':peer_bytes/MIB/DURATION_S,'total_mib_s':(in_bytes+out_bytes+peer_bytes)/MIB/DURATION_S,
      'context_misses':ctxmiss,'offloaded_frames':offloads,'migration_control_bytes':mig,
      'max_node_busy_fraction':max(n.busy/final for n in ns),
      'ingress_busy_fraction':ib/final,'egress_busy_fraction':ob/final,'peer_busy_fraction':pb/final,
      'stale_rejected':stale
    }

def simulate(streams,policy,u=UTIL_DEFAULT,bw=BW_DEFAULT,phase_mode='staggered'):
    if policy in ('stream_owner','frame_parallel','adaptive_v9'):
        return simulate_simple(streams,policy,u,bw,phase_mode)
    if policy=='stage_pipeline':return simulate_stage(streams,u,bw,phase_mode)
    if policy=='tile_parallel':return simulate_tile(streams,u,bw,phase_mode)
    raise ValueError(policy)

def write_csv(path,rows):
    fieldnames=[]
    seen=set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key); fieldnames.append(key)
    with open(path,'w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fieldnames);w.writeheader();w.writerows(rows)

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--outdir',default='.');args=ap.parse_args()
    d=Path(args.outdir);d.mkdir(parents=True,exist_ok=True)
    primary=[simulate(s,p,.40,125,phase) for phase in ('staggered','synchronized')
             for s in STREAM_COUNTS for p in POLICIES]
    write_csv(d/'f4_scheduler_sweep.csv',primary)

    bws=[simulate(s,p,.40,bw,'staggered') for s in (4,8,12,16,20,24,32)
         for p in ('stream_owner','adaptive_v9','frame_parallel') for bw in (25,50,100,125,200,400)]
    write_csv(d/'f4_bandwidth_sweep.csv',bws)

    utils=[simulate(s,p,u,125,'staggered') for s in (4,8,12,16,20,24,32,40)
           for p in ('stream_owner','adaptive_v9') for u in (.20,.30,.40,.50,.60,.70)]
    write_csv(d/'f4_utilization_sweep.csv',utils)

    capacity=[]
    for phase in ('staggered','synchronized'):
      for p in POLICIES:
        rows=[r for r in primary if r['phase_mode']==phase and r['policy']==p]
        ok=[r for r in rows if r['deadline_misses']==0 and r['throughput_fps_per_stream']>=59]
        capacity.append({'phase_mode':phase,'policy':p,
                         'max_zero_miss_streams_tested':max((r['streams'] for r in ok),default=0)})
    write_csv(d/'f4_capacity_frontier.csv',capacity)

    focus=[r for r in primary if r['phase_mode']=='staggered' and r['streams'] in (4,8,16,24,32)]
    assert all(r['stale_rejected']==r['streams'] for r in primary if r['policy'] in ('stream_owner','frame_parallel','adaptive_v9'))
    assert any(r['policy']=='frame_parallel' and r['context_misses']>0 for r in primary)
    summary={
      'status':'F4_SCHEDULER_SOFTWARE_SIMULATION_ONLY',
      'workload':'360p10->1080p60 median F3 component arithmetic',
      'streams_tested':STREAM_COUNTS,
      'policies':POLICIES,
      'capacity_frontier':capacity,
      'focus':focus,
      'limitations':[
        'No RV1126 silicon timing, trained reconstruction quality, memory bandwidth, thermal, or MPP multi-stream encode capacity proof.',
        '12 Mbit/s output per stream is an assumed transport bitrate.',
        'Full-duplex controller ingress/egress plus a separate peer-fabric queue are modeled.',
        'Stream phases are tested both perfectly synchronized and uniformly staggered.',
        'Adaptive policy uses a 3 ms offload hysteresis and 4 KiB control charge; this is a scheduler hypothesis, not V9 state migration.',
        'Stage/tile intermediate formats and tile seam-repair cost are modeling assumptions.'
      ]
    }
    (d/'f4_summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps({'status':'F4_PASS','capacity_frontier':capacity,
      'selected':[ {k:r[k] for k in ('streams','policy','phase_mode','throughput_fps_per_stream','deadline_misses','p95_latency_ms','total_mib_s','context_misses','offloaded_frames','max_node_busy_fraction')}
                   for r in focus if r['policy'] in ('stream_owner','adaptive_v9','frame_parallel')]},indent=2))
if __name__=='__main__':main()
