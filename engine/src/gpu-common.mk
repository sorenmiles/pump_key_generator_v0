NVCC:=nvcc

# --- GPU architecture --------------------------------------------------------
# The upstream defaults (compute_35 / sm_37) were REMOVED in CUDA 12 and will
# not compile on a modern toolkit. Override both of these for your card, e.g.:
#
#     make GPU_ARCHS=sm_86 GPU_PTX_ARCH=compute_86
#
# ../../build.sh auto-detects your GPU's compute capability and passes the right
# values automatically, so you normally don't set these by hand. The defaults
# below (Turing, compute_75) JIT-run on any newer GPU and build on CUDA 10+.
GPU_PTX_ARCH?=compute_75
GPU_ARCHS?=sm_75
GPU_CFLAGS:=--gpu-code=$(GPU_ARCHS),$(GPU_PTX_ARCH) --gpu-architecture=$(GPU_PTX_ARCH)

# -Werror dropped on purpose: a single benign warning from a newer nvcc/gcc
# would otherwise abort the build on a machine we can't iterate on quickly.
CFLAGS_release:=--ptxas-options=-v $(GPU_CFLAGS) -O3 -Xcompiler "-Wall -fPIC -Wno-strict-aliasing"
CFLAGS_debug:=$(CFLAGS_release) -g
CFLAGS:=$(CFLAGS_$V)
