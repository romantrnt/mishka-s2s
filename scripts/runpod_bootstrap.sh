#!/usr/bin/env bash
set -Eeuo pipefail

# DEPRECATED: this historical bootstrap is intentionally not a setup entrypoint.
# Use scripts/setup_runpod.sh on the pinned RunPod base image, then launch with
# scripts/runpod_start.sh. Keeping this stub prevents the old Torch 2.11 path
# from silently diverging from the canonical Torch 2.8 environment.
cat >&2 <<'EOF'
runpod_bootstrap.sh is deprecated and is not part of the canonical RunPod path.

Canonical setup:
  ./scripts/setup_runpod.sh
Canonical launch:
  ./scripts/runpod_start.sh
Base image:
  runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404

This deprecated entrypoint intentionally performs no dependency, CUDA, or Torch
installation. Do not use it for migration or production startup.
EOF
exit 2
