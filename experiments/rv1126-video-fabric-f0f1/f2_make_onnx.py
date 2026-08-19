#!/usr/bin/env python3
from pathlib import Path
import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper

OUT = Path(__file__).resolve().parent
np.random.seed(1126)

# Deliberately tiny fixed-shape residual corrector. Geometric warp/reprojection is
# OUTSIDE this graph. The graph only repairs a pre-warped RGB frame.
X = helper.make_tensor_value_info('input', TensorProto.FLOAT, [1, 3, 64, 64])
Y = helper.make_tensor_value_info('output', TensorProto.FLOAT, [1, 3, 64, 64])

def w(name, shape, scale=0.02):
    a = (np.random.randn(*shape).astype(np.float32) * scale)
    return numpy_helper.from_array(a, name=name)

def b(name, n):
    return numpy_helper.from_array(np.zeros((n,), dtype=np.float32), name=name)

inits = [
    w('w0', (16,3,3,3)), b('b0',16),
    w('wdw', (16,1,3,3)), b('bdw',16),
    w('w1', (3,16,1,1)), b('b1',3),
]
nodes = [
    helper.make_node('Conv', ['input','w0','b0'], ['f0'], pads=[1,1,1,1], strides=[1,1], name='conv_in'),
    helper.make_node('Relu', ['f0'], ['r0'], name='relu0'),
    helper.make_node('Conv', ['r0','wdw','bdw'], ['fdw'], pads=[1,1,1,1], strides=[1,1], group=16, name='depthwise3x3'),
    helper.make_node('Relu', ['fdw'], ['rdw'], name='relu1'),
    helper.make_node('Conv', ['rdw','w1','b1'], ['residual'], pads=[0,0,0,0], strides=[1,1], name='conv_out'),
    helper.make_node('Add', ['input','residual'], ['output'], name='residual_add'),
]
graph = helper.make_graph(nodes, 'rv1126_tiny_residual_f2', [X], [Y], inits)
model = helper.make_model(graph, producer_name='rk3588-rv1126-video-fabric-f2', opset_imports=[helper.make_operatorsetid('', 11)])
model.ir_version = 6
onnx.checker.check_model(model)
onnx.save(model, OUT/'rv1126_tiny_residual_f2.onnx')

# Calibration data uses deterministic PPMs so no image library is required.
cal = OUT/'f2_calibration'
cal.mkdir(exist_ok=True)
paths=[]
for i in range(8):
    yy,xx=np.mgrid[0:64,0:64]
    rgb=np.stack(((xx*4+i*13)%256,(yy*4+i*29)%256,((xx+yy)*2+i*47)%256),axis=-1).astype(np.uint8)
    p=cal/f'cal_{i:02d}.ppm'
    with p.open('wb') as f:
        f.write(b'P6\n64 64\n255\n'); f.write(rgb.tobytes())
    paths.append(str(p))
(OUT/'f2_dataset.txt').write_text('\n'.join(paths)+'\n')
print('F2_ONNX_CREATED', OUT/'rv1126_tiny_residual_f2.onnx')
print('F2_OPSET', 11, 'IR', model.ir_version)
print('F2_NODES', ','.join(n.op_type for n in nodes))
