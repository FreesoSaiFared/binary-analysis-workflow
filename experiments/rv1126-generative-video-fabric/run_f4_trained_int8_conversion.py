#!/usr/bin/env python3
"""Convert the trained F4 residual CNN with calibration matching its real input semantics.

This proves legacy RKNN-Toolkit 1.7.5 can quantize/export the already compiler-proven
Conv/ReLU/Add graph after training. Converter success is not RV1126 runtime image
quality and no host timing is RV1126 silicon timing.
"""
import argparse
import hashlib
import json
from pathlib import Path

from rknn.api import RKNN


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    model = Path(args.model)
    dataset = Path(args.dataset)
    manifest_path = Path(args.manifest)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(manifest_path.read_text())
    rknn_path = out / "rv1126_f4_trained_semantics_int8.rknn"

    expected_layout = [
        "left_rgb_r", "left_rgb_g", "left_rgb_b",
        "right_rgb_r", "right_rgb_g", "right_rgb_b",
        "interpolation_phase_byte",
    ]
    semantic_ok = (
        manifest.get("protocol") == "RV1126_F4_TRAINED_DECODER_CALIBRATION/1"
        and manifest.get("status") == "MATCHES_TRAINED_DECODER_INPUT_SEMANTICS"
        and manifest.get("sample_count") == 16
        and manifest.get("tensor", {}).get("shape_hwc") == [360, 640, 7]
        and manifest.get("tensor", {}).get("dtype") == "uint8"
        and manifest.get("tensor", {}).get("layout") == expected_layout
    )
    if not semantic_ok:
        raise SystemExit("RV1126_F4_CALIBRATION_SEMANTICS_MISMATCH")

    result = {
        "protocol": "RV1126_F4_TRAINED_INT8_CONVERSION/1",
        "target": "rv1126",
        "model_sha256": sha256(model),
        "dataset_sha256": sha256(dataset),
        "calibration_manifest_sha256": sha256(manifest_path),
        "calibration_protocol": manifest["protocol"],
        "calibration_status": manifest["status"],
        "calibration_samples": manifest["sample_count"],
        "calibration_layout": expected_layout,
        "do_quantization": True,
        "load_ret": None,
        "build_ret": None,
        "export_ret": None,
        "rknn_exists": False,
        "rknn_size": 0,
        "rknn_sha256": None,
        "exception": None,
        "quality_claim": False,
        "runtime_image_quality_claim": False,
        "rv1126_timing_claim": False,
    }

    rknn = RKNN(verbose=True)
    try:
        rknn.config(target_platform=["rv1126"])
        result["load_ret"] = int(rknn.load_onnx(
            model=str(model), inputs=["input"], input_size_list=[[7, 360, 640]], outputs=["residual"]
        ))
        if result["load_ret"] == 0:
            result["build_ret"] = int(rknn.build(do_quantization=True, dataset=str(dataset)))
        if result["build_ret"] == 0:
            result["export_ret"] = int(rknn.export_rknn(str(rknn_path)))
        if result["export_ret"] == 0 and rknn_path.exists():
            result["rknn_exists"] = True
            result["rknn_size"] = rknn_path.stat().st_size
            result["rknn_sha256"] = sha256(rknn_path)
    except Exception as exc:
        result["exception"] = repr(exc)
    finally:
        try:
            rknn.release()
        except Exception:
            pass

    result_path = out / "F4_TRAINED_INT8_CONVERSION_RESULT.json"
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print("F4_TRAINED_INT8_CONVERSION_RESULT", json.dumps(result, sort_keys=True))
    ok = (result["load_ret"], result["build_ret"], result["export_ret"], result["rknn_exists"]) == (0, 0, 0, True)
    print("RV1126_F4_TRAINED_INT8_CONVERTER_PASS" if ok else "RV1126_F4_TRAINED_INT8_CONVERTER_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
