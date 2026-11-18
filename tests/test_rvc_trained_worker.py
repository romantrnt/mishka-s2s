"""CPU helpers/protocol failures only; these tests do not run RVC inference."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/rvc_trained_worker.py"
spec = importlib.util.spec_from_file_location("mishka_trained_rvc_worker", SCRIPT)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class IndexFixture:
    nprobe = 1
    nlist = 100
    def __init__(self, fail=False, invalid=False):
        self.calls = []
        self.fail, self.invalid = fail, invalid
    def search(self, query, k):
        self.calls.append(self.nprobe)
        if self.fail:
            raise RuntimeError("fixture retrieval failure")
        ids = [[-1, 2]] if self.invalid or self.nprobe < self.nlist else [[1, 2]]
        return np.array([[0, .2]], dtype=np.float32), np.array(ids)


def test_recorded_nprobe_retry_restores_probe_and_counts_real_queries():
    evidence = worker.counters()
    raw = IndexFixture()
    index = worker.StrictIndex(raw, np, evidence)
    distances, ids = index.search(np.zeros((1, 768), dtype=np.float32), 2)
    assert raw.calls == [16, 100] and raw.nprobe == 16
    assert (ids >= 0).all() and (distances > 0).all()
    assert evidence["index_queries"] == evidence["full_probe_retries"] == 1
    assert evidence["index_failures"] == 0


@pytest.mark.parametrize("mode", ["invalid", "exception"])
def test_invalid_retrieval_cannot_count_as_conversion(mode):
    evidence = worker.counters()
    index = worker.StrictIndex(IndexFixture(fail=mode == "exception", invalid=mode == "invalid"), np, evidence)
    before = dict(evidence)
    with pytest.raises(RuntimeError):
        index.search(np.zeros((1, 768)), 2)
    assert evidence["index_failures"] == 1
    evidence["model_calls"] = 1  # Upstream may swallow the index error and infer.
    with pytest.raises(RuntimeError, match="not proved"):
        worker.require_conversion_evidence(before, evidence)


@pytest.mark.parametrize("model_calls,queries,failures", [(0, 1, 0), (1, 0, 0), (1, 1, 1)])
def test_passthrough_or_skipped_retrieval_fails_evidence_gate(model_calls, queries, failures):
    before = worker.counters()
    after = {**before, "model_calls": model_calls, "index_queries": queries, "index_failures": failures}
    with pytest.raises(RuntimeError):
        worker.require_conversion_evidence(before, after)


def test_valid_evidence_is_per_request_delta():
    before = dict(index_reads=3, index_queries=7, index_failures=0, full_probe_retries=2, model_calls=4)
    after = {key: value + int(key in {"index_reads", "index_queries", "model_calls"}) for key, value in before.items()}
    assert worker.require_conversion_evidence(before, after) == dict(index_reads=1,index_queries=1,index_failures=0,full_probe_retries=0,model_calls=1)


def test_asset_digest_failure_precedes_import_or_gpu_work(tmp_path, monkeypatch):
    model, index = tmp_path / "model.pth", tmp_path / "retrieval.index"
    model.write_bytes(b"fixture model")
    index.write_bytes(b"fixture index")
    monkeypatch.setattr(worker, "MODEL_SHA256", hashlib.sha256(model.read_bytes()).hexdigest())
    monkeypatch.setattr(worker, "INDEX_SHA256", hashlib.sha256(index.read_bytes()).hexdigest())
    monkeypatch.setattr(worker, "ASSET_HASHES", {})
    monkeypatch.setattr(worker, "SOURCE_HASHES", {})
    worker.validate_assets(tmp_path, model, index)
    model.write_bytes(b"wrong model")
    with pytest.raises(ValueError, match="identity unavailable"):
        worker.validate_assets(tmp_path, model, index)


def test_historical_cli_compatible_defaults_and_explicit_invalid_settings():
    required = ["--project-root", "/private/source", "--model", "/private/model", "--index", "/private/index"]
    args = worker.arguments(required)
    assert args.index_rate == .75 and args.filter_radius == 3 and args.f0_method == "rmvpe"
    for setting in [["--index-rate", "0"], ["--index-rate", "nan"], ["--f0-method", "harvest"], ["--filter-radius", "5"]]:
        with pytest.raises(SystemExit):
            worker.arguments(required + setting)


def test_real_worker_missing_assets_is_bounded_protocol_failure(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), "--project-root", str(tmp_path),
        "--model", str(tmp_path / "private_missing_model"), "--index", str(tmp_path / "private_missing_index")],
        capture_output=True, text=True, timeout=3)
    assert result.returncode == 1
    reply = json.loads(result.stdout)
    assert reply == {"event": "error", "error": "trained_rvc_start_failed"}
    assert str(tmp_path) not in result.stdout and "ready" not in result.stdout
