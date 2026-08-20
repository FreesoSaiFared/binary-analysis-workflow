#!/usr/bin/env python3
"""Train/evaluate the compiler-proven RV1126 9.1570176-GOP residual CNN.

The NPU graph is unchanged from the proven F2 positive graph: fixed 1x7x360x640
input, 16-channel stem, four 16-channel residual blocks, 3-channel output,
Conv/ReLU/Add only. The CNN predicts an RGB residual around deterministic linear
interpolation. Training/evaluation uses real native-60 video frame pairs and real
intermediate ground truth. Reference PTQ is a documented fake-INT8 simulation,
not RKNN runtime quality and not RV1126 silicon timing.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import random
import re
import subprocess
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

W, H = 640, 360
C = 3
FRAME_BYTES = W * H * C
SOURCE_FPS = 60
RATES = (5, 10, 15)
STEPS = {5: 12, 10: 6, 15: 4}
EVAL_START_FRAME = 600
EVAL_FRAMES = 180
TRAIN_LO, TRAIN_HI = 840, 1680       # 14s..28s
VAL_LO, VAL_HI = 1680, 1800          # 28s..30s
CAL_LO, CAL_HI = 1920, 2160          # 32s..36s
EXPECTED_SOURCE_FRAMES = 2340


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd, capture=False):
    print("RUN", " ".join(map(str, cmd)), flush=True)
    return subprocess.run(
        list(map(str, cmd)), check=True, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def phase_values(offset: int, step: int):
    true_phase = offset / step
    phase_byte = int(round(255.0 * true_phase))
    return phase_byte, phase_byte / 255.0


class ResidualBlock(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv2d(16, 16, 3, padding=1, bias=True)
        self.conv2 = nn.Conv2d(16, 16, 3, padding=1, bias=True)

    def forward(self, x):
        y = F.relu(self.conv1(x))
        y = self.conv2(y)
        return F.relu(x + y)


class ResidualCNN(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv_in = nn.Conv2d(7, 16, 3, padding=1, bias=True)
        self.blocks = nn.ModuleList([ResidualBlock() for _ in range(4)])
        self.conv_out = nn.Conv2d(16, 3, 3, padding=1, bias=True)
        nn.init.zeros_(self.conv_out.weight)
        nn.init.zeros_(self.conv_out.bias)

    def forward(self, x):
        y = F.relu(self.conv_in(x))
        for block in self.blocks:
            y = block(y)
        return self.conv_out(y)


def model_contract() -> dict:
    macs = (
        W * H * 7 * 16 * 3 * 3
        + 4 * 2 * W * H * 16 * 16 * 3 * 3
        + W * H * 16 * 3 * 3 * 3
    )
    return {
        "input_nchw": [1, 7, H, W],
        "output_nchw": [1, 3, H, W],
        "channels": 16,
        "residual_blocks": 4,
        "conv_nodes": 10,
        "relu_nodes": 9,
        "add_nodes": 4,
        "macs_per_frame": macs,
        "gop_multiply_plus_add": macs * 2 / 1e9,
        "allowed_onnx_ops": ["Conv", "Relu", "Add"],
    }


def decode_source_360(src: Path, raw: Path):
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
        "-an", "-vf", f"scale={W}:{H}:flags=lanczos", "-pix_fmt", "rgb24",
        "-f", "rawvideo", raw,
    ])
    expected = EXPECTED_SOURCE_FRAMES * FRAME_BYTES
    got = raw.stat().st_size
    if got != expected:
        raise RuntimeError(f"360p raw size mismatch: {got} != {expected}")


def build_eval_reference_1080(src: Path, dst: Path):
    vf = (
        f"trim=start_frame={EVAL_START_FRAME}:end_frame={EVAL_START_FRAME + EVAL_FRAMES},"
        "setpts=PTS-STARTPTS,scale=1920:1080:flags=lanczos,format=yuv420p"
    )
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
        "-an", "-vf", vf, "-frames:v", str(EVAL_FRAMES),
        "-c:v", "ffv1", "-level", "3", dst,
    ])
    cp = run([
        "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
        "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1:nk=1", dst,
    ], capture=True)
    if int(cp.stdout.strip()) != EVAL_FRAMES:
        raise RuntimeError("eval reference is not exactly 180 frames")


def make_input(left: np.ndarray, right: np.ndarray, phase_norm: float, device="cpu"):
    l = torch.from_numpy(left.copy()).permute(2, 0, 1).float().div_(255.0)
    r = torch.from_numpy(right.copy()).permute(2, 0, 1).float().div_(255.0)
    p = torch.full((1, left.shape[0], left.shape[1]), float(phase_norm), dtype=torch.float32)
    return torch.cat([l, r, p], dim=0).unsqueeze(0).to(device)


def sample_batch(frames, rng: random.Random, batch: int, crop: int, lo: int, hi: int):
    xs, bases, targets = [], [], []
    for _ in range(batch):
        rate = rng.choice(RATES)
        step = STEPS[rate]
        left_i = rng.randrange(lo, hi - step - 1)
        offset = rng.randrange(1, step)
        right_i = left_i + step
        target_i = left_i + offset
        y0 = rng.randrange(0, H - crop + 1)
        x0 = rng.randrange(0, W - crop + 1)
        left = np.asarray(frames[left_i, y0:y0+crop, x0:x0+crop], dtype=np.uint8)
        right = np.asarray(frames[right_i, y0:y0+crop, x0:x0+crop], dtype=np.uint8)
        gt = np.asarray(frames[target_i, y0:y0+crop, x0:x0+crop], dtype=np.uint8)
        phase_byte, phase_norm = phase_values(offset, step)
        if rng.random() < 0.5:
            left = left[:, ::-1].copy(); right = right[:, ::-1].copy(); gt = gt[:, ::-1].copy()
        l = torch.from_numpy(left.copy()).permute(2,0,1).float() / 255.0
        r = torch.from_numpy(right.copy()).permute(2,0,1).float() / 255.0
        g = torch.from_numpy(gt.copy()).permute(2,0,1).float() / 255.0
        p = torch.full((1,crop,crop), phase_norm, dtype=torch.float32)
        x = torch.cat([l,r,p],0)
        base = l * (1.0-phase_norm) + r * phase_norm
        xs.append(x); bases.append(base); targets.append(g)
    return torch.stack(xs), torch.stack(bases), torch.stack(targets)


def fixed_validation_samples(frames, count=24, crop=64):
    rng = random.Random(99173)
    samples = []
    for _ in range(count):
        x,b,g = sample_batch(frames,rng,1,crop,VAL_LO,VAL_HI)
        samples.append((x,b,g))
    return samples


def validation_mse(model, samples):
    total = 0.0
    with torch.no_grad():
        for x,b,g in samples:
            pred = torch.clamp(b + model(x), 0.0, 1.0)
            total += F.mse_loss(pred,g).item()
    return total / len(samples)


def train_model(frames, steps: int, batch: int, crop: int, seed: int):
    random.seed(seed); np.random.seed(seed); torch.manual_seed(seed)
    torch.set_num_threads(max(1, os.cpu_count() or 1))
    model = ResidualCNN().train()
    opt = torch.optim.Adam(model.parameters(), lr=2e-3)
    rng = random.Random(seed + 17)
    val = fixed_validation_samples(frames)
    initial_val = validation_mse(model.eval(), val)
    model.train()
    best_val = initial_val
    best_state = copy.deepcopy(model.state_dict())
    trace = [{"step":0,"validation_mse":initial_val}]
    last_loss = None
    for step_i in range(1, steps+1):
        x,b,g = sample_batch(frames,rng,batch,crop,TRAIN_LO,TRAIN_HI)
        opt.zero_grad(set_to_none=True)
        pred = torch.clamp(b + model(x), 0.0, 1.0)
        loss = F.mse_loss(pred,g)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        last_loss = float(loss.item())
        if step_i % 50 == 0 or step_i == steps:
            vm = validation_mse(model.eval(), val)
            trace.append({"step":step_i,"training_mse":last_loss,"validation_mse":vm})
            print("TRAIN_PROGRESS", json.dumps(trace[-1], sort_keys=True), flush=True)
            if vm < best_val:
                best_val = vm
                best_state = copy.deepcopy(model.state_dict())
            model.train()
    model.load_state_dict(best_state)
    model.eval()
    nonzero = float(model.conv_out.weight.detach().abs().sum().item())
    return model, {
        "seed": seed, "steps": steps, "batch": batch, "crop": crop,
        "initial_validation_mse": initial_val, "best_validation_mse": best_val,
        "last_training_mse": last_loss, "conv_out_abs_weight_sum": nonzero,
        "trace": trace,
    }


def calibration_specs():
    specs=[]
    for i in range(16):
        rate=RATES[i % len(RATES)]
        step=STEPS[rate]
        usable=(CAL_HI-CAL_LO-step-1)
        left=CAL_LO + ((i * 37) % max(1,usable))
        offset=1 + ((i * 5 + rate) % (step-1))
        pb,pn=phase_values(offset,step)
        specs.append({"index":i,"rate":rate,"step":step,"left":left,"right":left+step,
                      "target":left+offset,"offset":offset,"phase_byte":pb,"phase_norm":pn})
    return specs


def write_calibration(frames, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    rows=[]; samples=[]
    for s in calibration_specs():
        left=np.asarray(frames[s["left"]],dtype=np.uint8)
        right=np.asarray(frames[s["right"]],dtype=np.uint8)
        packed=np.empty((H,W,7),dtype=np.uint8)
        packed[...,0:3]=left; packed[...,3:6]=right; packed[...,6]=s["phase_byte"]
        p=out/f"trained_semantics_cal_{s['index']:02d}.npy"
        np.save(p,packed)
        rows.append(str(p.resolve()))
        rec=dict(s); rec["file"]=p.name; rec["sha256"]=sha256(p); samples.append(rec)
    dataset=out/"dataset.txt"; dataset.write_text("\n".join(rows)+"\n")
    manifest={
        "protocol":"RV1126_F4_TRAINED_DECODER_CALIBRATION/1",
        "status":"MATCHES_TRAINED_DECODER_INPUT_SEMANTICS",
        "sample_count":16,
        "tensor":{
            "shape_hwc":[H,W,7],"dtype":"uint8",
            "layout":["left_rgb_r","left_rgb_g","left_rgb_b","right_rgb_r","right_rgb_g","right_rgb_b","interpolation_phase_byte"],
            "phase_definition":"round(255 * intermediate_frame_offset / authoritative_interval_frames)",
            "model_normalization":"all seven uint8 channels divided by 255 in the training-domain model; scaling is algebraically folded into first-layer weights for raw-domain ONNX",
            "pair_semantics":"left/right are real 360p RGB authoritative frames separated by 60/source_fps frames; target is the real intermediate frame",
        },
        "split":{"calibration_frame_range":[CAL_LO,CAL_HI],"seconds":[CAL_LO/60.0,CAL_HI/60.0]},
        "samples":samples,
        "dataset_sha256":sha256(dataset),
        "quality_claim":"reference PTQ uses these exact samples; RKNN export alone is not a runtime quality claim",
    }
    mp=out/"F4_CALIBRATION_MANIFEST.json"; mp.write_text(json.dumps(manifest,indent=2,sort_keys=True)+"\n")
    return manifest


def update_range(ranges, name, tensor):
    mn=float(tensor.min().item()); mx=float(tensor.max().item())
    if name not in ranges: ranges[name]=[mn,mx]
    else:
        ranges[name][0]=min(ranges[name][0],mn); ranges[name][1]=max(ranges[name][1],mx)


def forward_collect(model, x, ranges):
    update_range(ranges,"input",x)
    y=F.relu(model.conv_in(x)); update_range(ranges,"stem",y)
    for i,b in enumerate(model.blocks):
        c1=F.relu(b.conv1(y)); update_range(ranges,f"b{i}_c1",c1)
        c2=b.conv2(c1); update_range(ranges,f"b{i}_c2",c2)
        y=F.relu(y+c2); update_range(ranges,f"b{i}_out",y)
    out=model.conv_out(y); update_range(ranges,"output",out)
    return out


def qdq_activation(t, mm):
    mn=min(float(mm[0]),0.0); mx=max(float(mm[1]),0.0)
    if mx <= mn + 1e-12: return t
    scale=(mx-mn)/255.0
    zp=int(round(-mn/scale)); zp=max(0,min(255,zp))
    q=torch.clamp(torch.round(t/scale + zp),0,255)
    return (q-zp)*scale


def qdq_weight_per_out(w):
    shape=[w.shape[0]]+[1]*(w.ndim-1)
    maxabs=w.detach().abs().reshape(w.shape[0],-1).max(dim=1).values
    scale=torch.where(maxabs>0,maxabs/127.0,torch.ones_like(maxabs)).reshape(shape)
    q=torch.clamp(torch.round(w/scale),-127,127)
    return q*scale


def quantized_copy(model):
    q=copy.deepcopy(model).eval()
    with torch.no_grad():
        for m in q.modules():
            if isinstance(m,nn.Conv2d): m.weight.copy_(qdq_weight_per_out(m.weight))
    return q


def forward_fake_int8(qmodel, x, ranges):
    y=qdq_activation(x,ranges["input"])
    y=F.relu(qmodel.conv_in(y)); y=qdq_activation(y,ranges["stem"])
    for i,b in enumerate(qmodel.blocks):
        c1=F.relu(b.conv1(y)); c1=qdq_activation(c1,ranges[f"b{i}_c1"])
        c2=b.conv2(c1); c2=qdq_activation(c2,ranges[f"b{i}_c2"])
        y=F.relu(y+c2); y=qdq_activation(y,ranges[f"b{i}_out"])
    out=qmodel.conv_out(y); return qdq_activation(out,ranges["output"])


def collect_calibration_ranges(model, frames):
    ranges={}
    with torch.no_grad():
        for s in calibration_specs():
            left=np.asarray(frames[s["left"]],dtype=np.uint8)
            right=np.asarray(frames[s["right"]],dtype=np.uint8)
            x=make_input(left,right,s["phase_norm"])
            forward_collect(model,x,ranges)
    return {k:{"min":v[0],"max":v[1]} for k,v in ranges.items()}


def raw_domain_model(model):
    raw=copy.deepcopy(model).eval()
    with torch.no_grad():
        raw.conv_in.weight.div_(255.0)
        raw.conv_out.weight.mul_(255.0)
        raw.conv_out.bias.mul_(255.0)
    return raw


def verify_raw_fold(model, raw_model, frames):
    maxerr=0.0
    with torch.no_grad():
        for rate in RATES:
            step=STEPS[rate]; left_i=EVAL_START_FRAME+7; offset=max(1,step//2)
            right_i=left_i+step
            pb,pn=phase_values(offset,step)
            left=np.asarray(frames[left_i],dtype=np.uint8); right=np.asarray(frames[right_i],dtype=np.uint8)
            x_norm=make_input(left,right,pn)
            packed=np.empty((H,W,7),dtype=np.float32)
            packed[...,0:3]=left; packed[...,3:6]=right; packed[...,6]=pb
            x_raw=torch.from_numpy(packed).permute(2,0,1).unsqueeze(0)
            a=model(x_norm)*255.0; b=raw_model(x_raw)
            maxerr=max(maxerr,float((a-b).abs().max().item()))
    if maxerr > 2e-3: raise RuntimeError(f"raw-domain fold mismatch {maxerr}")
    return maxerr


def export_onnx(raw_model, path: Path):
    dummy=torch.zeros((1,7,H,W),dtype=torch.float32)
    torch.onnx.export(raw_model,dummy,str(path),opset_version=11,input_names=["input"],output_names=["residual"],
                      do_constant_folding=True,dynamic_axes=None)
    import onnx
    m=onnx.load(str(path)); onnx.checker.check_model(m)
    ops={}
    for n in m.graph.node: ops[n.op_type]=ops.get(n.op_type,0)+1
    allowed={"Conv","Relu","Add"}
    bad=sorted(set(ops)-allowed)
    if bad: raise RuntimeError(f"unexpected ONNX ops {bad}: {ops}")
    if ops.get("Conv")!=10 or ops.get("Relu")!=9 or ops.get("Add")!=4:
        raise RuntimeError(f"graph count mismatch {ops}")
    return ops


def write_candidate_raw(path: Path, frames_u8):
    with path.open("wb") as f:
        for a in frames_u8: f.write(np.ascontiguousarray(a,dtype=np.uint8).tobytes())
    expected=EVAL_FRAMES*FRAME_BYTES
    if path.stat().st_size!=expected: raise RuntimeError("candidate raw size mismatch")


def ffmpeg_metric(reference: Path, cand_raw: Path, work: Path, label: str):
    cand_video=work/f"{label}.mkv"
    run(["ffmpeg","-hide_banner","-loglevel","error","-y","-f","rawvideo","-pix_fmt","rgb24",
         "-s:v",f"{W}x{H}","-r","60","-i",cand_raw,"-frames:v",str(EVAL_FRAMES),
         "-vf","scale=1920:1080:flags=lanczos,format=yuv420p","-c:v","ffv1","-level","3",cand_video])
    cp=run(["ffmpeg","-hide_banner","-i",reference,"-i",cand_video,"-lavfi","[0:v][1:v]psnr","-f","null","-"],capture=True)
    m=re.findall(r"average:([0-9.]+)",cp.stderr); psnr=float(m[-1])
    cp=run(["ffmpeg","-hide_banner","-i",reference,"-i",cand_video,"-lavfi","[0:v][1:v]ssim","-f","null","-"],capture=True)
    m=re.findall(r"All:([0-9.]+)",cp.stderr); ssim=float(m[-1])
    cand_video.unlink(missing_ok=True); cand_raw.unlink(missing_ok=True)
    return {"psnr_db":psnr,"ssim":ssim}


def construct_cases(model, qmodel, ranges_plain, frames, rate):
    step=STEPS[rate]
    eval0=EVAL_START_FRAME
    spatial=[]; hold=[]; linear=[]; dec=[]; qdec=[]
    ranges={k:[v["min"],v["max"]] for k,v in ranges_plain.items()}
    with torch.no_grad():
        for n in range(EVAL_FRAMES):
            gt=np.asarray(frames[eval0+n],dtype=np.uint8)
            spatial.append(gt)
            left_rel=(n//step)*step
            left_i=eval0+left_rel
            right_i=left_i+step
            offset=n-left_rel
            left=np.asarray(frames[left_i],dtype=np.uint8)
            if offset==0:
                hold.append(gt); linear.append(gt); dec.append(gt); qdec.append(gt); continue
            right=np.asarray(frames[right_i],dtype=np.uint8)
            pb,pn=phase_values(offset,step)
            hold.append(left)
            lin=np.clip(np.rint(left.astype(np.float32)*(1.0-pn)+right.astype(np.float32)*pn),0,255).astype(np.uint8)
            linear.append(lin)
            x=make_input(left,right,pn)
            base=(torch.from_numpy(left.copy()).permute(2,0,1).float()/255.0)*(1.0-pn) + (torch.from_numpy(right.copy()).permute(2,0,1).float()/255.0)*pn
            res=model(x)[0]
            pred=torch.clamp(base+res,0,1).mul(255).round().byte().permute(1,2,0).cpu().numpy()
            dec.append(pred)
            qres=forward_fake_int8(qmodel,x,ranges)[0]
            qpred=torch.clamp(base+qres,0,1).mul(255).round().byte().permute(1,2,0).cpu().numpy()
            qdec.append(qpred)
    return {"spatial":spatial,"hold":hold,"linear":linear,"decoder_float":dec,"decoder_ref_int8":qdec}


def evaluate(model, qmodel, ranges, frames, reference: Path, work: Path):
    results={}
    spatial_done=False
    for rate in RATES:
        cases=construct_cases(model,qmodel,ranges,frames,rate)
        if not spatial_done:
            p=work/"candidate.raw"; write_candidate_raw(p,cases["spatial"])
            results["spatial_only_360p60"]=dict(authoritative_fps=60,**ffmpeg_metric(reference,p,work,"spatial_only_360p60"))
            spatial_done=True
        for name in ("hold","linear","decoder_float","decoder_ref_int8"):
            label=f"{name}_{rate}_to_60"
            p=work/"candidate.raw"; write_candidate_raw(p,cases[name])
            results[label]=dict(authoritative_fps=rate,**ffmpeg_metric(reference,p,work,label))
            print("QUALITY_RESULT",label,json.dumps(results[label],sort_keys=True),flush=True)
    return results


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--source",required=True); ap.add_argument("--out",required=True)
    ap.add_argument("--steps",type=int,default=800); ap.add_argument("--batch",type=int,default=8)
    ap.add_argument("--crop",type=int,default=64); ap.add_argument("--seed",type=int,default=1126)
    a=ap.parse_args()
    src=Path(a.source); out=Path(a.out); out.mkdir(parents=True,exist_ok=True)
    work=out/"work"; work.mkdir(exist_ok=True); evidence=out/"evidence"; evidence.mkdir(exist_ok=True)
    models=out/"models"; models.mkdir(exist_ok=True); corpus=out/"calibration"; corpus.mkdir(exist_ok=True)

    contract=model_contract()
    if abs(contract["gop_multiply_plus_add"]-9.1570176)>1e-9: raise RuntimeError(contract)
    raw360=work/"source_360.rgb"; decode_source_360(src,raw360)
    frames=np.memmap(raw360,dtype=np.uint8,mode="r",shape=(EXPECTED_SOURCE_FRAMES,H,W,3))
    reference=work/"reference_eval_1080.mkv"; build_eval_reference_1080(src,reference)

    model,training=train_model(frames,a.steps,a.batch,a.crop,a.seed)
    torch.save(model.state_dict(),models/"trained_residual_cnn_state.pt")
    cal_manifest=write_calibration(frames,corpus)
    ranges=collect_calibration_ranges(model,frames)
    qmodel=quantized_copy(model)
    raw_model=raw_domain_model(model)
    fold_error=verify_raw_fold(model,raw_model,frames)
    onnx_path=models/"rv1126_f4_trained_residual_raw_u8_semantics.onnx"
    onnx_ops=export_onnx(raw_model,onnx_path)
    results=evaluate(model,qmodel,ranges,frames,reference,work)

    comparisons={}
    for rate in RATES:
        h=results[f"hold_{rate}_to_60"]; l=results[f"linear_{rate}_to_60"]
        f=results[f"decoder_float_{rate}_to_60"]; q=results[f"decoder_ref_int8_{rate}_to_60"]
        comparisons[str(rate)]={
            "float_minus_hold":{"psnr_db":f["psnr_db"]-h["psnr_db"],"ssim":f["ssim"]-h["ssim"]},
            "float_minus_linear":{"psnr_db":f["psnr_db"]-l["psnr_db"],"ssim":f["ssim"]-l["ssim"]},
            "reference_int8_minus_float":{"psnr_db":q["psnr_db"]-f["psnr_db"],"ssim":q["ssim"]-f["ssim"]},
        }
    result={
        "protocol":"RV1126_F4_TRAINED_DECODER_QUALITY/1",
        "status":"REFERENCE_FRAMEWORK_TRAINED_QUALITY_MEASURED",
        "source":{"filename":src.name,"sha256":sha256(src),"native_fps":60,"source_frames":EXPECTED_SOURCE_FRAMES},
        "architecture":contract,
        "semantics":{
            "input_channels":["left_rgb_r","left_rgb_g","left_rgb_b","right_rgb_r","right_rgb_g","right_rgb_b","interpolation_phase_byte"],
            "input_training_domain":"RGB and phase byte normalized by 255",
            "phase_byte":"round(255 * intermediate_offset_frames / authoritative_interval_frames)",
            "output":"3-channel RGB residual in normalized training domain; final frame = clamp(linear(left,right,phase_byte/255) + residual, 0, 1)",
            "raw_onnx_domain":"input numeric 0..255; first-layer /255 and output-layer *255 scalings folded into Conv weights; output residual is in pixel-value units",
        },
        "splits":{
            "evaluation":{"frames":[600,780],"seconds":[10,13],"ground_truth":"real native-60 intermediate frames; 1080p reference spatially scaled from original 4K only"},
            "training":{"frames":[TRAIN_LO,TRAIN_HI],"seconds":[TRAIN_LO/60.0,TRAIN_HI/60.0]},
            "validation":{"frames":[VAL_LO,VAL_HI],"seconds":[VAL_LO/60.0,VAL_HI/60.0]},
            "calibration":{"frames":[CAL_LO,CAL_HI],"seconds":[CAL_LO/60.0,CAL_HI/60.0]},
        },
        "training":training,
        "calibration_manifest_sha256":sha256(corpus/"F4_CALIBRATION_MANIFEST.json"),
        "reference_ptq":{
            "class":"fake INT8 reference simulation, not RKNN runtime",
            "weights":"symmetric signed int8 per-output-channel quantize/dequantize",
            "activations":"affine uint8 per-tensor quantize/dequantize using min/max from the exact 16 real calibration samples",
            "activation_ranges":ranges,
            "calibration_samples":16,
        },
        "export":{
            "onnx":onnx_path.name,"onnx_sha256":sha256(onnx_path),"onnx_ops":onnx_ops,
            "raw_domain_fold_max_abs_error":fold_error,
        },
        "results":results,
        "comparisons":comparisons,
        "scope_limits":[
            "Float and reference-PTQ quality are host reference-framework measurements, not RV1126 runtime measurements.",
            "The reference fake-INT8 simulation is not claimed bit-equivalent to RKNN quantization.",
            "RKNN converter success, if separately proven, does not establish RKNN runtime image quality.",
            "No GitHub-host, simulator, QEMU or CPU timing is RV1126 silicon timing.",
            "This first trained proof uses disjoint temporal splits of one verified open native-60 source and does not establish cross-video generalization."
        ]
    }
    rp=evidence/"F4_TRAINED_DECODER_QUALITY.json"; rp.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
    (evidence/"F4_REFERENCE_PTQ_RANGES.json").write_text(json.dumps(ranges,indent=2,sort_keys=True)+"\n")
    (evidence/"F4_MODEL_CONTRACT.json").write_text(json.dumps(contract,indent=2,sort_keys=True)+"\n")
    print("RV1126_F4_TRAINED_DECODER_QUALITY_PASS",json.dumps({
        "onnx_sha256":sha256(onnx_path),"best_validation_mse":training["best_validation_mse"],
        "comparisons":comparisons},sort_keys=True),flush=True)
    return 0

if __name__=="__main__": raise SystemExit(main())
