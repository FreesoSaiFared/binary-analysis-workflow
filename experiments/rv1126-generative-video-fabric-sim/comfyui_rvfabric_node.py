"""ComfyUI-compatible control-plane adapter for the native rvfabricd pixel daemon.

The helper is dependency-light except NumPy. The ComfyUI class imports torch only
inside execution so CI can exercise the exact socket protocol without installing
ComfyUI or PyTorch. Production timing belongs to rvfabricd/workers, not Python.
"""
from __future__ import annotations

import json
import socket
from typing import Any

import numpy as np

PROTOCOL = "RVFABRIC_COMFY/1"


def _readline(stream) -> bytes:
    line = stream.readline()
    if not line:
        raise RuntimeError("rvfabricd closed before response header")
    return line


def _read_exact(stream, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise RuntimeError(f"rvfabricd payload truncated {len(data)}/{size}")
        data.extend(chunk)
    return bytes(data)


def expand_rgb_numpy(
    images_u8: np.ndarray,
    source_fps: int,
    target_fps: int = 60,
    host: str = "127.0.0.1",
    port: int = 19000,
    generation: int = 1,
    timeout_seconds: float = 10.0,
    return_metadata: bool = False,
):
    images = np.asarray(images_u8)
    if images.dtype != np.uint8:
        raise TypeError(f"expected uint8, got {images.dtype}")
    if images.ndim != 4 or images.shape[-1] != 3:
        raise ValueError(f"expected [frames,height,width,3], got {images.shape}")
    if not images.flags.c_contiguous:
        images = np.ascontiguousarray(images)
    frame_count, height, width, _ = images.shape
    request = {
        "protocol": PROTOCOL,
        "generation": int(generation),
        "source_fps": int(source_fps),
        "target_fps": int(target_fps),
        "width": int(width),
        "height": int(height),
        "frame_count": int(frame_count),
    }
    with socket.create_connection((host, int(port)), timeout=timeout_seconds) as sock:
        sock.settimeout(timeout_seconds)
        stream = sock.makefile("rb")
        sock.sendall(json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n")
        sock.sendall(images.tobytes(order="C"))
        metadata: dict[str, Any] = json.loads(_readline(stream))
        if metadata.get("protocol") != PROTOCOL or metadata.get("status") != "ok":
            raise RuntimeError(f"bad rvfabricd response: {metadata}")
        expected_bytes = int(metadata["payload_bytes"])
        payload = _read_exact(stream, expected_bytes)
    output = np.frombuffer(payload, dtype=np.uint8).reshape(
        int(metadata["output_frames"]), int(metadata["height"]), int(metadata["width"]), 3
    ).copy()
    return (output, metadata) if return_metadata else output


class RVFabricExpand:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "source_fps": ("INT", {"default": 10, "min": 1, "max": 59}),
                "target_fps": ("INT", {"default": 60, "min": 2, "max": 240}),
                "generation": ("INT", {"default": 1, "min": 1}),
            },
            "optional": {
                "host": ("STRING", {"default": "127.0.0.1"}),
                "port": ("INT", {"default": 19000, "min": 1, "max": 65535}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("expanded_images",)
    FUNCTION = "expand"
    CATEGORY = "RV Fabric"

    def expand(self, images, source_fps, target_fps, generation, host="127.0.0.1", port=19000):
        import torch

        if images.ndim != 4 or images.shape[-1] != 3:
            raise ValueError(f"ComfyUI IMAGE batch must be [B,H,W,3], got {tuple(images.shape)}")
        source = torch.clamp(images.detach().cpu(), 0.0, 1.0).mul(255.0).round().to(torch.uint8).numpy()
        expanded = expand_rgb_numpy(source, source_fps, target_fps, host, port, generation)
        result = torch.from_numpy(expanded).to(torch.float32).div_(255.0)
        return (result,)


NODE_CLASS_MAPPINGS = {"RVFabricExpand": RVFabricExpand}
NODE_DISPLAY_NAME_MAPPINGS = {"RVFabricExpand": "RV Fabric Expand"}
