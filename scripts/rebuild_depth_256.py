"""Trace Depth Anything V2 Small at 256x256 — ~4x fewer ops than native 518."""
import torch, time, os
from transformers import AutoModelForDepthEstimation

MODEL = "depth-anything/Depth-Anything-V2-Small-hf"
model = AutoModelForDepthEstimation.from_pretrained(MODEL).eval()

class Wrap(torch.nn.Module):
    def __init__(self, m): super().__init__(); self.m=m
    def forward(self, x): return self.m(x).predicted_depth

wrapped = Wrap(model)
dummy = torch.randn(1, 3, 256, 256)
with torch.no_grad():
    out = wrapped(dummy)
    print(f"out shape at 256: {out.shape}")
    traced = torch.jit.trace(wrapped, dummy, strict=False)
    traced.save("/tmp/depth-fix/depth_v2_small_256.pt")
size = os.path.getsize("/tmp/depth-fix/depth_v2_small_256.pt")/1e6
print(f"TorchScript file: {size:.1f} MB")
