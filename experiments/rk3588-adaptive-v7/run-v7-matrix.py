#!/usr/bin/env python3
from pathlib import Path
import subprocess, sys, time
H=Path(__file__).resolve().parent
results=H/"results"
results.mkdir(exist_ok=True)
cases=[(0,4),(0,0),(0,1),(0,2),(0,3),(1,4),(1,0),(1,1),(1,2),(1,3)]
start=int(sys.argv[1]) if len(sys.argv)>1 else 0
end=int(sys.argv[2]) if len(sys.argv)>2 else len(cases)
for i,(s,c) in enumerate(cases[start:end],start):
    marker=results/f"case-{i:02d}.done"
    if marker.exists():
        print(f"V7_SKIP case={i} scenario={s} candidate={c}")
        continue
    t=time.time()
    subprocess.run([str(H/"run-one-v7.sh"),str(s),str(c)],cwd=H,check=True)
    marker.write_text(f"{time.time()-t:.6f}\n")
    print(f"V7_CASE_DONE index={i} scenario={s} candidate={c} elapsed={time.time()-t:.3f}")
print(f"V7_BATCH_PASS start={start} end={end}")
