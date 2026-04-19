# Vulkan patches for Mali-G78 (Pixel 6 / Tensor G1)

`llama.cpp-b8840-mali-descriptor-pool.patch` — stock llama.cpp at tag b8840 aborts on Mali-G78
with `GGML_ASSERT(ctx->descriptor_set_idx < ctx->descriptor_sets.size()) failed` when running
Q4_0 1B-class models. Patch grows the descriptor pool on demand. Apply, then build:

```
cd llama.cpp
patch -p1 < .../llama.cpp-b8840-mali-descriptor-pool.patch

export ANDROID_NDK_HOME=/path/to/android-ndk-r26d
cmake -B build-android \
  -DCMAKE_TOOLCHAIN_FILE=$ANDROID_NDK_HOME/build/cmake/android.toolchain.cmake \
  -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-28 \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_VULKAN=ON -DGGML_OPENMP=OFF \
  -DVulkan_INCLUDE_DIR=/path/to/Vulkan-Headers/include \
  -DVulkan_LIBRARY=$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/lib/aarch64-linux-android/28/libvulkan.so \
  -DVulkan_GLSLC_EXECUTABLE=$ANDROID_NDK_HOME/shader-tools/linux-x86_64/glslc \
  -DCMAKE_CXX_FLAGS="-isystem /path/to/SPIRV-Headers/include" \
  -GNinja
cmake --build build-android --target llama-bench llama-cli -j$(nproc)
```

Dependencies: Vulkan-Headers (master), SPIRV-Headers (master), Android NDK r26d.

Runtime verdict on Pixel 6 Mali-G78 (see `logs/pixel6_vulkan_accelerated_2026-04-19.log`):
the backend runs correctly after this patch, but is ~2.5× slower than 4-thread CPU for
token generation on Q4_0 1B models, and ~10× slower for prompt processing. Mali-G78 has
no matrix cores, only 32 KiB shared memory, warp size 16 — hardware that doesn't win
against NEON for memory-bandwidth-bound quantized matvec. The acceleration path on
Tensor G1 is the Edge TPU via NNAPI, which requires `.tflite` models (not GGUF).
