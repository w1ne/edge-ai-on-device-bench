import torch, os, sys, time

MODEL = "depth-anything/Depth-Anything-V2-Small-hf"
print(f"[1/3] Loading {MODEL} from HuggingFace...")
t0 = time.time()
from transformers import AutoImageProcessor, AutoModelForDepthEstimation
proc = AutoImageProcessor.from_pretrained(MODEL)
model = AutoModelForDepthEstimation.from_pretrained(MODEL)
model.eval()
print(f"  loaded in {time.time()-t0:.1f}s")

# Input shape 1x3x518x518 (model's native)
dummy = torch.randn(1, 3, 518, 518)

print("[2/3] Tracing to TorchScript...")
t0 = time.time()
with torch.no_grad():
    out = model(dummy)
    print(f"  forward ran, output keys: {list(out.keys())}, shape: {out.predicted_depth.shape}")
    # Use a simpler wrapper so trace returns tensor directly
    class Wrap(torch.nn.Module):
        def __init__(self, m): super().__init__(); self.m=m
        def forward(self, x): return self.m(x).predicted_depth
    wrapped = Wrap(model)
    traced = torch.jit.trace(wrapped, dummy, strict=False)
    traced.save("depth_v2_small.pt")
print(f"  traced + saved in {time.time()-t0:.1f}s")

size = os.path.getsize("depth_v2_small.pt")/1e6
print(f"[3/3] TorchScript file: depth_v2_small.pt, {size:.1f} MB")
