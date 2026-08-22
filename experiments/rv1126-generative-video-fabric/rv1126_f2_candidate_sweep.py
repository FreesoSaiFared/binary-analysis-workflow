#!/usr/bin/env python3
"""Candidate-specific workload sweep for the F2 360p tiny residual CNN.

Imports the F0 fabric model and substitutes the statically counted 9.1570176 GOP
candidate. This is a software workload result, not RKNN conversion or silicon timing.
"""
from pathlib import Path
import argparse,csv,json,importlib.util,sys

GOP=9.1570176

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--f0',default='rv1126_video_fabric_f0.py'); ap.add_argument('--out',default='.'); a=ap.parse_args()
    spec=importlib.util.spec_from_file_location('rvf0',a.f0)
    m=importlib.util.module_from_spec(spec); sys.modules['rvf0']=m; spec.loader.exec_module(m)
    out=Path(a.out); out.mkdir(parents=True,exist_ok=True)
    thresholds=[]
    for sf in (5,10,15):
        hit=None
        for i in range(200,701):
            u=i/1000
            r=m.run_case(sf,4,u,1000,GOP,10.0)
            if r['deadline_miss_rate']==0:
                hit=r; break
        thresholds.append({
            'source_fps':sf,
            'min_utilization_zero_miss_in_model':None if hit is None else hit['effective_utilization'],
            'p95_latency_ms_at_threshold':None if hit is None else hit['p95_latency_ms'],
            'link_load_at_threshold':None if hit is None else hit['link_load'],
            'steady_state_fps_at_threshold':None if hit is None else hit['steady_state_fps'],
        })
    cases=[]
    for sf in (5,10,15):
      for nodes in (4,8,16):
       for util in (.2,.4,.45,.5,.6,.7):
        for link in (100,250,1000):
         cases.append(m.run_case(sf,nodes,util,link,GOP,10.0))
    payload={'status':'F2_TINY_CNN_WORKLOAD_SWEEP_SOFTWARE_MODEL','candidate_gop':GOP,
             'not_rknn_converted':True,'not_silicon_timing':True,
             'thresholds_4nodes_1gbe':thresholds,'cases':cases}
    (out/'RV1126_F2_TINY_CNN_FABRIC_SWEEP.json').write_text(json.dumps(payload,indent=2))
    fields=['source_fps','node_count','effective_utilization','link_mbps','effective_tops_total','required_tops','compute_load','link_load','steady_state_fps','p50_latency_ms','p95_latency_ms','p99_latency_ms','deadline_miss_rate','stale_generations_rejected','bottleneck']
    with open(out/'RV1126_F2_TINY_CNN_FABRIC_SWEEP.csv','w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for r in cases: w.writerow({k:r[k] for k in fields})
    print(json.dumps(thresholds,indent=2))

if __name__=='__main__': main()
