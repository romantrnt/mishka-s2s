from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE_IMAGE = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"


def _text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_dependency_lock_is_exact_and_excludes_base_torch():
    lock = _text("requirements-production.lock")
    assert "numpy==2.1.2" in lock
    assert "langgraph==1.2.2" in lock
    assert "torch==" not in lock
    assert "torchvision==" not in lock
    assert "torchaudio==" not in lock
    assert "nvidia-" not in lock
    assert "triton==" not in lock
    pinned = [line for line in lock.splitlines() if line and not line.startswith("#")]
    assert pinned and all("==" in line for line in pinned)
    assert len({line.split("==", 1)[0].lower() for line in pinned}) == len(pinned)


def test_regression_overlay_is_exactly_pinned():
    overlay = _text("requirements-test.lock")
    entries = [line for line in overlay.splitlines() if line and not line.startswith("#")]
    assert entries and all("==" in line for line in entries)
    assert "pytest==9.1.1" in overlay
    assert "pytest>=" not in overlay
    assert "pytest<" not in overlay


def test_runpod_setup_uses_pinned_base_and_never_installs_torch():
    setup = _text("scripts/setup_runpod.sh")
    assert f'BASE_IMAGE="{BASE_IMAGE}"' in setup
    assert BASE_IMAGE in setup
    assert "pip install torch" not in setup
    assert "torch==" not in setup
    assert "download.pytorch.org" not in setup
    assert "--system-site-packages" in setup
    assert "requirements-production.lock" in setup
    assert "--no-deps -r" in setup
    assert "pip install --upgrade pip" not in setup


def test_runpod_bootstrap_is_explicitly_deprecated_and_inert():
    bootstrap = _text("scripts/runpod_bootstrap.sh")
    assert "DEPRECATED" in bootstrap
    assert "setup_runpod.sh" in bootstrap
    assert "runpod_start.sh" in bootstrap
    assert "pip install" not in bootstrap
    assert "torch==" not in bootstrap
    assert "download.pytorch.org" not in bootstrap


def test_canonical_start_is_fixed_to_port_8000():
    start = _text("scripts/runpod_start.sh")
    assert "MISHKA_PORT=8000" in start
    assert "scripts/run_stand.py" in start
    assert "8015" not in start


def test_setup_is_safe_to_repeat_without_destructive_cleanup():
    setup = _text("scripts/setup_runpod.sh")
    assert "mkdir -p" in setup
    assert "[[ -s" in setup
    assert "--upgrade-strategy only-if-needed" not in setup
    assert "rm -rf" not in setup


def test_clean_setup_has_fail_closed_ubuntu_gpu_preflight_and_dry_run():
    setup = _text("scripts/setup_clean.sh")
    assert "VERSION_ID:-}\" == \"24.04" in setup
    assert "VERSION_CODENAME:-}\" == \"noble" in setup
    assert "nvidia-smi" in setup
    assert "MIN_DRIVER_MAJOR=\"570\"" in setup
    assert "MIN_CUDA_MINOR=\"8\"" in setup
    assert "MISHKA_DRY_RUN" in setup
    assert "requirements-production.lock" in setup
    assert "requirements-test.lock" in setup
    assert "pip install --upgrade pip" not in setup
    assert "--upgrade-strategy only-if-needed" not in setup


def test_clean_setup_does_not_overwrite_incompatible_torch():
    setup = _text("scripts/setup_clean.sh")
    assert "an incompatible Torch is already installed" in setup
    assert "--no-deps \"$TORCH_SPEC\"" in setup
    assert "download.pytorch.org/whl/cu128" in setup
    assert "apt-get install" in setup
    assert "cuda-toolkit" not in setup

