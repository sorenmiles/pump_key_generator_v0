#!/usr/bin/env bash
#
# Build the CUDA "pump" vanity engine.
#
# Auto-detects your GPU's compute capability (via nvidia-smi) so the binary is
# compiled natively for your card. Override by exporting GPU_ARCHS / GPU_PTX_ARCH
# before running, e.g.  GPU_ARCHS=sm_86 GPU_PTX_ARCH=compute_86 ./build.sh
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="$HERE/engine"

echo ">> Checking toolchain..."
command -v nvcc >/dev/null 2>&1 || {
  echo "ERROR: 'nvcc' not found. Install the CUDA Toolkit and put it on PATH." >&2
  echo "       (Verify with: nvcc --version)" >&2
  exit 1
}
nvcc --version | sed -n '4p' || true

# --- Determine target architecture ------------------------------------------
if [[ -n "${GPU_ARCHS:-}" && -n "${GPU_PTX_ARCH:-}" ]]; then
  echo ">> Using arch from environment: GPU_ARCHS=$GPU_ARCHS GPU_PTX_ARCH=$GPU_PTX_ARCH"
elif command -v nvidia-smi >/dev/null 2>&1 \
     && CAP="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d ' .')" \
     && [[ -n "$CAP" ]]; then
  GPU_ARCHS="sm_${CAP}"
  GPU_PTX_ARCH="compute_${CAP}"
  echo ">> Detected GPU compute capability ${CAP} -> GPU_ARCHS=$GPU_ARCHS GPU_PTX_ARCH=$GPU_PTX_ARCH"
else
  echo ">> Could not auto-detect GPU; falling back to Makefile defaults (sm_75)."
  echo "   If the build or run fails, set the arch for your card explicitly, e.g.:"
  echo "     GPU_ARCHS=sm_86 GPU_PTX_ARCH=compute_86 ./build.sh"
  GPU_ARCHS=""   # let the Makefile defaults apply
fi

# --- Build -------------------------------------------------------------------
echo ">> Building engine in $ENGINE ..."
if [[ -n "${GPU_ARCHS}" ]]; then
  make -C "$ENGINE" GPU_ARCHS="$GPU_ARCHS" GPU_PTX_ARCH="$GPU_PTX_ARCH"
else
  make -C "$ENGINE"
fi

BIN="$ENGINE/src/release/cuda_ed25519_vanity"
if [[ -x "$BIN" ]]; then
  echo ""
  echo ">> Build OK: $BIN"
  echo "   Run the search + DB storage with:  python3 find_pump_keys.py"
else
  echo "ERROR: expected binary not found at $BIN" >&2
  exit 1
fi
