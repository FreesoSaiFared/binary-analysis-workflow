#!/usr/bin/env python3
from pathlib import Path
import re, json
H=Path(__file__).resolve().parent
R=H/"results"
cases=[(0,4),(0,0),(0,1),(0,2),(0,3),(1,4),(1,0),(1,1),(1,2),(1,3)]
expected_total={0:{0:16384,1:12288,2:20480,3:24576},1:{0:16384,1:24576,2:20480,3:12288}}
expected_page={0:{0:16384,1:4096,2:12288,3:16384},1:{0:16384,1:16384,2:12288,3:4096}}
records=[]
for i,(s,c) in enumerate(cases):
    d=R/f"s{s}-c{c}"
    task=(d/"task.txt").read_text().strip(); cost=(d/"cost.txt").read_text().strip()
    def num(k,line):
        m=re.search(rf"\b{k}=(0x[0-9a-fA-F]+|[0-9]+)",line)
        if not m: raise SystemExit(f"missing {k}: {line}")
        return int(m.group(1),0)
    rec={"index":i,"scenario":s,"requested":c,"target":num("target",task),"remote_bytes":num("remote_bytes",task),"remote_xfers":num("remote_xfers",task),"local_hits":num("local_hits",task),"modeled_state_bytes":num("modeled_state_bytes",task),"modeled_total":num("modeled_total",task),"checksum":hex(num("checksum",task)),"predicted":num("predicted",cost)}
    records.append(rec)
    if c==4:
        want=1 if s==0 else 3
        assert rec["target"]==want and rec["predicted"]==want
    else:
        assert rec["target"]==c
        assert rec["remote_bytes"]==expected_page[s][c]
        assert rec["modeled_total"]==expected_total[s][c]
forced={s:{} for s in (0,1)}
for r in records:
    if r["requested"]<4: forced[r["scenario"]][r["requested"]]=r["modeled_total"]
winners={s:min(forced[s],key=forced[s].get) for s in (0,1)}
assert winners=={0:1,1:3},winners
checks={r["checksum"] for r in records}
assert checks=={"0x4a9f55cc4def6f8d"},checks
out={"status":"PASS","winner_initial":1,"winner_mutated":3,"checksum":"0x4a9f55cc4def6f8d","records":records}
(H/"V7_RESULT.json").write_text(json.dumps(out,indent=2)+"\n")
print("RKMESH_V7_PREDICTION_MATCHES_EMPIRICAL winner_initial=1 winner_mutated=3")
print("RKMESH_QEMU_4ARM64_ADAPTIVE_PLACEMENT_V7_PASS")
