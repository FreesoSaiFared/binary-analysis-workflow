#!/usr/bin/env python3
from pathlib import Path
import re,json
H=Path(__file__).resolve().parent;R=H/'results'
names=['neg_hyst','neg_greedy','pos_hyst','pos_stay']
expected={
 'neg_hyst':dict(iterations=6,migrations=0,final_host=0,task_remote_bytes=73728,modeled_migration_bytes=0,modeled_total=73728,mutation_control_bytes=45056,targets=[0,0,0,0,0,0],migrated=[0]*6,best=[1,3,1,3,1,3]),
 'neg_greedy':dict(iterations=6,migrations=6,final_host=3,task_remote_bytes=49152,modeled_migration_bytes=49152,modeled_total=98304,mutation_control_bytes=45056,targets=[1,3,1,3,1,3],migrated=[1]*6,best=[1,3,1,3,1,3]),
 'pos_hyst':dict(iterations=7,migrations=1,final_host=1,task_remote_bytes=73728,modeled_migration_bytes=8192,modeled_total=81920,mutation_control_bytes=12288,targets=[0,0,1,1,1,1,1],migrated=[0,0,1,0,0,0,0],best=[1,1,1,1,1,1,3]),
 'pos_stay':dict(iterations=7,migrations=0,final_host=0,task_remote_bytes=86016,modeled_migration_bytes=0,modeled_total=86016,mutation_control_bytes=12288,targets=[0]*7,migrated=[0]*7,best=[1,1,1,1,1,1,3]),
}
def val(line,k):
 m=re.search(rf'\b{k}=([0-9]+)',line)
 if not m:raise SystemExit(f'missing {k}: {line}')
 return int(m.group(1))
out={}
for name in names:
 e=expected[name];d=R/name;summary=(d/'summary.txt').read_text().strip();dec=(d/'decisions.txt').read_text().splitlines();tasks=(d/'tasks.txt').read_text().splitlines()
 for k in ['iterations','migrations','final_host','task_remote_bytes','modeled_migration_bytes','modeled_total','mutation_control_bytes']:
  assert val(summary,k)==e[k],(name,k,summary)
 assert len(dec)==e['iterations'] and len(tasks)==e['iterations']
 assert [val(x,'current_after') for x in dec]==e['targets']
 assert [val(x,'migrated') for x in dec]==e['migrated']
 assert [val(x,'best') for x in dec]==e['best']
 for t in tasks:
  m=re.search(r'checksum=(0x[0-9a-f]+)',t);assert m and m.group(1)=='0x4a9f55cc4def6f8d',t
 out[name]={k:val(summary,k) for k in ['iterations','migrations','final_host','task_remote_bytes','modeled_migration_bytes','modeled_total','mutation_control_bytes']}
assert out['neg_hyst']['modeled_total'] < out['neg_greedy']['modeled_total']
assert out['pos_hyst']['modeled_total'] < out['pos_stay']['modeled_total']
assert out['neg_greedy']['modeled_total']-out['neg_hyst']['modeled_total']==24576
assert out['pos_stay']['modeled_total']-out['pos_hyst']['modeled_total']==4096
result={'status':'PASS','checksum':'0x4a9f55cc4def6f8d','negative_hysteresis_saves_vs_greedy':24576,'positive_hysteresis_saves_vs_stay':4096,'cases':out}
(H/'V8_RESULT.json').write_text(json.dumps(result,indent=2)+'\n')
print('RKMESH_V8_HYSTERESIS_BEATS_GREEDY savings=24576')
print('RKMESH_V8_AMORTIZATION_BEATS_STAY savings=4096')
print('RKMESH_QEMU_4ARM64_HYSTERESIS_V8_PASS')
