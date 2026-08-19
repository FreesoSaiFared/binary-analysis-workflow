#!/usr/bin/env python3
"""Sweep fixed-shape residual-CNN design points against ideal RV1126 live windows.
Software arithmetic only; no RKNN conversion or silicon timing is implied.
"""
import csv,json,math,argparse
from pathlib import Path
DERATE=.85*.90*.95

def macs(w,h,c,b,inc=7):
    return h*w*inc*c*9 + b*2*h*w*c*c*9 + h*w*c*3*9

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--out',default='.'); a=ap.parse_args(); out=Path(a.out); out.mkdir(parents=True,exist_ok=True)
    rows=[]
    for w,h in ((640,360),(960,540),(1280,720)):
      for c in (4,8,12,16,24,32):
       for b in (1,2,3,4,6,8):
        m=macs(w,h,c,b); g=2*m/1e9
        r={'width':w,'height':h,'channels':c,'blocks':b,'GMAC':m/1e9,'GOP':g,'peak_int8_activation_MB':h*w*c/1e6}
        for u in (.2,.4,.7):
            cap=2*u*DERATE*(16.667-.15)
            r[f'ideal_npUs_for_16.667ms_u{int(u*100)}']=math.ceil(g/cap)
            r[f'ideal_margin_GOP_1npu_u{int(u*100)}']=cap-g
        rows.append(r)
    with open(out/'RV1126_F2_CNN_PARETO_SWEEP.csv','w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    (out/'RV1126_F2_CNN_PARETO_SWEEP.json').write_text(json.dumps(rows,indent=2))
    print('F2_CNN_PARETO_SWEEP cases=',len(rows))

if __name__=='__main__': main()
