#!/usr/bin/env bash
# Prints 1 when Docker containers here can use an NVIDIA GPU, otherwise 0.
# With --vram, prints the largest GPU's memory in GB instead (nothing if none).
# Used by the Makefile, so it must never fail loudly.
set -u

smi=""
# In WSL the Windows driver provides nvidia-smi under /usr/lib/wsl/lib, which
# isn't on PATH for systemd services.
for candidate in nvidia-smi /usr/lib/wsl/lib/nvidia-smi; do
  if command -v "$candidate" >/dev/null 2>&1; then
    smi=$(command -v "$candidate")
    break
  fi
done

if [[ "${1:-}" == "--vram" ]]; then
  [[ -n "$smi" ]] || exit 0
  "$smi" --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null |
    awk 'BEGIN { max = 0 } { if ($1 + 0 > max) max = $1 + 0 } END { if (max > 0) printf "%.1f\n", max / 1024 }'
  exit 0
fi

# Docker hands GPUs to containers through this hook from the NVIDIA Container Toolkit.
if [[ -n "$smi" ]] && "$smi" -L >/dev/null 2>&1 && command -v nvidia-container-runtime-hook >/dev/null 2>&1; then
  echo 1
else
  echo 0
fi
