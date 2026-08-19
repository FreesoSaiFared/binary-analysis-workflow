#!/usr/bin/env python3
from pathlib import Path
from rknn.api import RKNN

D = Path(__file__).resolve().parent
ONNX = str(D/'rv1126_tiny_residual_f2.onnx')
DATA = str(D/'f2_dataset.txt')

def compile_one(quantized: bool, out_name: str):
    rknn = RKNN(verbose=True)
    print(f'F2_CONFIG_BEGIN quantized={int(quantized)}')
    ret = rknn.config(mean_values=[[0,0,0]], std_values=[[255,255,255]], reorder_channel='0 1 2', target_platform=['rv1126'])
    if ret not in (None,0): raise RuntimeError(f'config ret={ret}')
    print('F2_LOAD_ONNX_BEGIN')
    ret = rknn.load_onnx(model=ONNX, inputs=['input'], input_size_list=[[3,64,64]], outputs=['output'])
    if ret != 0: raise RuntimeError(f'load_onnx ret={ret}')
    print('F2_BUILD_BEGIN')
    # RKNN Toolkit 1.7.5 uses the dataset to construct input metadata even for
    # non-quantized builds; Rockchip's own RV1126 examples pass dataset.txt with
    # do_quantization=False. Use the identical deterministic dataset in both probes.
    ret = rknn.build(do_quantization=quantized, dataset=DATA)
    if ret != 0: raise RuntimeError(f'build ret={ret}')
    print('F2_EXPORT_BEGIN')
    out = str(D/out_name)
    ret = rknn.export_rknn(out)
    if ret != 0: raise RuntimeError(f'export ret={ret}')
    rknn.release()
    print(f'F2_RKNN_BUILD_PASS quantized={int(quantized)} output={out_name}')

compile_one(False, 'rv1126_tiny_residual_f2_fp.rknn')
compile_one(True, 'rv1126_tiny_residual_f2_int8.rknn')
print('RV1126_F2_OFFLINE_COMPILER_ACCEPTANCE_PASS')
print('F2_SCOPE software compiler/operator contract only; no RV1126 silicon timing or NPU-utilization proof')
