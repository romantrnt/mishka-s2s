"""Print GPU/PyTorch diagnostics without pretending unavailable hardware is ready."""
from __future__ import annotations

import json
import shutil


def main() -> int:
    result = {"nvidia_smi": bool(shutil.which("nvidia-smi")), "torch_installed": False, "cuda_available": False, "devices": []}
    try:
        import torch
        result["torch_installed"] = True
        result["cuda_available"] = bool(torch.cuda.is_available())
        if result["cuda_available"]:
            result["devices"] = [torch.cuda.get_device_name(index) for index in range(torch.cuda.device_count())]
    except ImportError:
        result["reason"] = "PyTorch is not installed in this environment"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["cuda_available"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
