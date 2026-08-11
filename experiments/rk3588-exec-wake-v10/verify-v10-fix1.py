#!/usr/bin/env python3
from pathlib import Path
import re,json
H=Path(__file__).resolve().parent;R=H/'results';names=['migrate','stay']
state=['0x5b48fce9c4e70c21','0x0602e7fe82ac09b0','0x4755f563da69ad10','0xb240c559d07ccbfa','0x4c896ddd8f2bf61f','0xc5fa77f3ce77cc9d','0x4240fa999ccf4304','0xa1d675e2b67b913c']
exe=['0x52873de4b5d717a5','0xdf6b7368b129dbf0','0x96f5ef13329cd4b6','0x6f8f994c6e77ee2c','0x403d62f650204440','0x2e66ac98f8281fbd','0xed4c469a9851cb1d']
expected={
'migrate':dict(iterations=7,migrations=1,final_host=1,task_page_bytes=73728,state_transfer_bytes=8192,state_xfers=1,measured_total=81920,mutation_control_bytes=12288,page_local_hits=10,state_local_hits=6,exec_valid_wakes=1,exec_stale_wakes=1,old_host_excluded=1,exec_runs=1,doorbells=47,barrier_arrivals=112,parked0=3,targets=[0,0,1,1,1,1,1],migrated=[0,0,1,0,0,0,0],best=[1,1,1,1,1,1,3]),
'stay':dict(iterations=7,migrations=0,final_host=0,task_page_bytes=86016,state_transfer_bytes=0,state_xfers=0,measured_total=86016,mutation_control_bytes=12288,page_local_hits=7,state_local_hits=7,exec_valid_wakes=0,exec_stale_wakes=0,old_host_excluded=0,exec_runs=0,doorbells=48,barrier_arrivals=112,parked0=0,targets=[0]*7,migrated=[0]*7,best=[1,1,1,1,1,1,3])}
def val(line,k):
 m=re.search(rf'\b{k}=([0-9]+)',line)
 if not m:raise SystemExit(f'missing {k}: {line}')
 return int(m.group(1))
def hx(line,k):
 m=re.search(rf'\b{k}=(0x[0-9a-f]+)',line)
 if not m:raise SystemExit(f'missing {k}: {line}')
 return m.group(1)
out={}
for name in names:
 e=expected[name];d=R/name;summary=(d/'summary.txt').read_text().strip();dec=(d/'decisions.txt').read_text().splitlines();tasks=(d/'tasks.txt').read_text().splitlines();xf=(d/'state-transfers.txt').read_text().splitlines() if (d/'state-transfers.txt').exists() else []
 for k in ['iterations','migrations','final_host','task_page_bytes','state_transfer_bytes','state_xfers','measured_total','mutation_control_bytes','page_local_hits','state_local_hits','exec_valid_wakes','exec_stale_wakes','old_host_excluded','exec_runs','doorbells','barrier_arrivals','parked0']:
  assert val(summary,k)==e[k],(name,k,summary)
 assert val(summary,'final_state_gen')==e['iterations'];assert hx(summary,'final_state_checksum')==state[e['iterations']]
 assert len(dec)==7 and len(tasks)==7;assert [val(x,'current_after') for x in dec]==e['targets'];assert [val(x,'migrated') for x in dec]==e['migrated'];assert [val(x,'best') for x in dec]==e['best']
 assert len(xf)==e['state_xfers'];assert all(val(x,'bytes')==8192 and val(x,'state_gen')==2 for x in xf)
 seen=set()
 for t in tasks:
  i=val(t,'iter');assert 0<=i<7 and i not in seen,(name,i,t);seen.add(i);assert hx(t,'page_checksum')=='0x4a9f55cc4def6f8d';assert hx(t,'exec_checksum')==exe[i];assert hx(t,'state_after')==state[i+1]
 assert seen==set(range(7)),(name,seen)
 logs=[(d/f'node{i}.log').read_text(errors='replace') for i in range(4)]
 byid={}
 for txt in logs:
  m=re.search(r'RKMESH_V10_USER_READY id=([0-3])\b',txt)
  assert m is not None,(name,'missing logical id')
  lid=int(m.group(1));assert lid not in byid,(name,'duplicate logical id',lid);byid[lid]=txt
 assert set(byid)==set(range(4)),(name,sorted(byid))
 if name=='migrate':
  n0=byid[0];n1=byid[1]
  assert n0.count('RKMESH_V10_EXEC_PARKED id=0 gen=2 destination=1')==1
  assert n0.count('RKMESH_V10_OLD_HOST_EXCLUDED id=0 gen=2 new_owner=1')==1
  assert n0.count('RKMESH_V10_OLD_HOST_COMMIT_REJECTED id=0 iter=2 errno=1')==1
  marks=['RKMESH_V10_EXEC_WAKE_STALE_IGNORED id=1 expected_gen=2','RKMESH_V10_EXEC_WAKE_VALID id=1 gen=2 dest=1','RKMESH_V10_STATE_WAKE_COMPLETE id=1 gen=2','RKMESH_V10_STATE_ACQUIRED id=1 iter=2','RKMESH_V10_TASK_RESULT mode=0 iter=2 target=1']
  pos=[n1.find(x) for x in marks];assert all(x>=0 for x in pos),(marks,pos);assert pos==sorted(pos),pos
  assert n1.count('RKMESH_V10_EXEC_WAKE_STALE_IGNORED')==1 and n1.count('RKMESH_V10_EXEC_WAKE_VALID')==1
 else:
  alltxt=''.join(logs);assert 'RKMESH_V10_EXEC_WAKE_' not in alltxt;assert 'RKMESH_V10_STATE_TRANSFER' not in alltxt;assert 'RKMESH_V10_EXEC_PARKED' not in alltxt
 out[name]={k:val(summary,k) for k in ['iterations','migrations','final_host','task_page_bytes','state_transfer_bytes','state_xfers','measured_total','mutation_control_bytes','exec_valid_wakes','exec_stale_wakes','old_host_excluded','exec_runs','doorbells','barrier_arrivals']}
assert out['migrate']['measured_total']<out['stay']['measured_total'];assert out['stay']['measured_total']-out['migrate']['measured_total']==4096
res={'status':'PASS','page_task_checksum':'0x4a9f55cc4def6f8d','migration_generation':2,'migration_destination':1,'stale_wake_negative_control':'PASS','old_host_exclusion':'PASS','measured_savings_vs_stay':4096,'cases':out};(H/'V10_RESULT.json').write_text(json.dumps(res,indent=2)+'\n')
print('RKMESH_V10_STALE_WAKE_REJECTED generation=1 expected_generation=2')
print('RKMESH_V10_OLD_HOST_EXCLUDED generation=2 source=0 destination=1')
print('RKMESH_V10_GENERATION_BOUND_WAKE_PASS generation=2 destination=1')
print('RKMESH_V10_AMORTIZATION_BEATS_STAY measured_savings=4096')
print('RKMESH_QEMU_4ARM64_EXEC_WAKE_V10_PASS')
