#!/usr/bin/env python3
"""Build fixed-shape ONNX controls for the original RV1126 legacy RKNN converter.

Positive: Conv + ReLU + Add only, opset 11.
Negative A: same graph plus ONNX Resize.
Negative B: same graph plus a custom WarpLike op in a custom domain; conversion must reject
or explicitly classify it, never silently pass it as NPU-native.
"""
from pathlib import Path
import argparse
import numpy as np
import onnx
from onnx import helper, TensorProto, numpy_helper


def weight(name, shape, seed):
    rng=np.random.RandomState(seed)
    a=(rng.randn(*shape)*0.01).astype(np.float32)
    return numpy_helper.from_array(a,name=name)


def conv(nodes,inits,x,y,cin,cout,name,seed,relu=True):
    w=f"{name}_w"; b=f"{name}_b"; raw=f"{name}_raw"
    inits += [weight(w,(cout,cin,3,3),seed),numpy_helper.from_array(np.zeros((cout,),dtype=np.float32),name=b)]
    nodes.append(helper.make_node("Conv",[x,w,b],[raw],name=name,pads=[1,1,1,1],strides=[1,1]))
    if relu:
        nodes.append(helper.make_node("Relu",[raw],[y],name=f"{name}_relu"))
    else:
        nodes[-1].output[0]=y


def build(path: Path, variant: str):
    h,w=360,640
    inp=helper.make_tensor_value_info("input",TensorProto.FLOAT,[1,7,h,w])
    nodes=[]; inits=[]
    conv(nodes,inits,"input","stem",7,16,"conv_in",1,True)
    x="stem"
    seed=10
    for block in range(4):
        c1=f"b{block}_c1"; c2=f"b{block}_c2"; out=f"b{block}_out"
        conv(nodes,inits,x,c1,16,16,f"res{block}_conv1",seed,True); seed+=1
        conv(nodes,inits,c1,c2,16,16,f"res{block}_conv2",seed,False); seed+=1
        nodes.append(helper.make_node("Add",[x,c2],[out],name=f"res{block}_add"))
        nodes.append(helper.make_node("Relu",[out],[f"{out}_relu"],name=f"res{block}_relu"))
        x=f"{out}_relu"
    conv(nodes,inits,x,"rgb",16,3,"conv_out",99,False)
    output_name="rgb"

    if variant == "resize":
        # opset-11 Resize, sizes supplied as initializer. Deliberately keep resize inside graph.
        roi=numpy_helper.from_array(np.array([],dtype=np.float32),name="resize_roi")
        scales=numpy_helper.from_array(np.array([],dtype=np.float32),name="resize_scales")
        sizes=numpy_helper.from_array(np.array([1,3,720,1280],dtype=np.int64),name="resize_sizes")
        inits += [roi,scales,sizes]
        nodes.append(helper.make_node("Resize",["rgb","resize_roi","resize_scales","resize_sizes"],["output"],name="resize_negative",mode="linear",coordinate_transformation_mode="half_pixel"))
        output_name="output"
        outshape=[1,3,720,1280]
    elif variant == "warp":
        # Deliberately unsupported custom operator: converter must fail closed/classify.
        nodes.append(helper.make_node("WarpLike",["rgb"],["output"],name="warp_negative",domain="rv1126.test"))
        output_name="output"; outshape=[1,3,h,w]
    else:
        outshape=[1,3,h,w]

    out=helper.make_tensor_value_info(output_name,TensorProto.FLOAT,outshape)
    graph=helper.make_graph(nodes,f"rv1126_f2_{variant}",[inp],[out],inits)
    model=helper.make_model(graph,producer_name="rk3588-rv1126-fabric",opset_imports=[helper.make_opsetid("",11),helper.make_opsetid("rv1126.test",1)])
    model.ir_version=7
    onnx.checker.check_model(model,check_custom_domain=False)
    onnx.save(model,str(path))


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--out",default="."); a=ap.parse_args()
    o=Path(a.out); o.mkdir(parents=True,exist_ok=True)
    for v in ("positive","resize","warp"):
        build(o/f"rv1126_f2_{v}.onnx",v)
        print(v,o/f"rv1126_f2_{v}.onnx")

if __name__=="__main__": main()
