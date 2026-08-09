#!/usr/bin/env python3
from pathlib import Path
import subprocess,time
H=Path(__file__).resolve().parent
for mode in range(4):
 t=time.time();subprocess.run([str(H/'run-one-v8.sh'),str(mode)],cwd=H,check=True);print(f'V8_CASE_DONE mode={mode} elapsed={time.time()-t:.3f}')
print('V8_MATRIX_EXECUTION_PASS')
