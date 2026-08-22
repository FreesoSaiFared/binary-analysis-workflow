#!/usr/bin/env python3
"""Run legacy RKNN-Toolkit conversion gates for F2.

Gate 1 is deliberately non-quantized: isolate graph/operator acceptance from
calibration-data handling. INT8 is a later gate only for graphs that pass here.
"""
from pathlib import Path
import argparse, json, traceback
from rknn.api import RKNN


def convert(model: Path, output: Path, output_name: str):
    rknn=RKNN(verbose=True)
    result={"model":model.name,"target":"rv1126","load_ret":None,"build_ret":None,"export_ret":None,"exception":None}
    try:
        rknn.config(target_platform=["rv1126"])
        ret=rknn.load_onnx(model=str(model),inputs=["input"],input_size_list=[[7,360,640]],outputs=[output_name])
        result["load_ret"]=int(ret)
        if ret != 0:
            return result
        ret=rknn.build(do_quantization=False)
        result["build_ret"]=int(ret)
        if ret != 0:
            return result
        ret=rknn.export_rknn(str(output))
        result["export_ret"]=int(ret)
        return result
    except Exception:
        result["exception"]=traceback.format_exc()
        return result
    finally:
        try: rknn.release()
        except Exception: pass


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--models",required=True); ap.add_argument("--out",required=True); a=ap.parse_args()
    m=Path(a.models); o=Path(a.out); o.mkdir(parents=True,exist_ok=True)
    cases=[("positive","rgb"),("resize","output"),("warp","output")]
    results=[]
    for name,outname in cases:
        r=convert(m/f"rv1126_f2_{name}.onnx",o/f"rv1126_f2_{name}.rknn",outname)
        results.append(r)
        print("F2_CONVERSION_RESULT",json.dumps(r,sort_keys=True))
    (o/"F2_CONVERSION_RESULTS.json").write_text(json.dumps(results,indent=2))

if __name__=="__main__": main()
