"""Read-only HF-01/HF-02 review reproductions; no network calls."""
from __future__ import annotations

import hashlib
import io
import json
import sys
import subprocess
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from core.planning.baseline_cli import main as baseline_main
from core.planning.baseline_models import CollectedBaseline, EvidenceClaim, PlannedItem, SourceObservation
from core.planning.baseline_probes import ProbeObservation, ProbeSpec, probe_endpoint, ProbeResponse
from core.planning.baseline_reconcile import reconcile_baseline
from core.planning.baseline_sources import _parse_markdown_dependencies
from spikes.runtime_choice.contracts import DriverCommand, LabConfig, RuntimeComparison, ScenarioResult
from spikes.runtime_choice.driver import main as driver_main
from spikes.runtime_choice.effect_store import NativeEffectStore
from spikes.runtime_choice.native_adapter import NativeAdapter

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)
HASH = "a" * 64
BASE = "b" * 40
WORK = REPO / ".factory/test_logs/hf-critical-review" / ("reproduction-" + uuid4().hex[:8])
WORK.mkdir(parents=True)
results: dict[str, object] = {}

def claim(claim_id: str, **changes: object) -> EvidenceClaim:
    data = dict(claim_id=claim_id, item_id="DF-11", dimension="implementation", assertion="positive", evidence_kind="document", source_id="fixture", locator="source.md#claim", source_hash=HASH, scope="same-scope", summary="synthetic claim")
    data.update(changes)
    return EvidenceClaim(**data)

source = SourceObservation(source_id="fixture", relative_path=Path("source.md"), sha256=HASH, status="read", observed_at=NOW)
planned = PlannedItem(item_id="DF-11", title="fixture", declared_status="planned", source_id="fixture", locator="source.md#claim")
collected = CollectedBaseline(observations=[source], planned_items=[planned])
def reconcile(claims=(), probes=()):
    return reconcile_baseline(collected, claims, base_sha=BASE, snapshot_id="synthetic", observed_at=NOW, probe_observations=probes)

remote = reconcile([claim("untrusted", dimension="integration", evidence_kind="remote_git", candidate_sha=BASE)])
results["unattested_remote_claim"] = remote.items[0].integration.value
conflict = reconcile([claim("positive"), claim("negative", assertion="negative")])
results["positive_negative_same_scope"] = {"assessment": conflict.items[0].implementation.value, "issues": [item.code for item in conflict.issues]}
probe = ProbeObservation(probe_id="mock", item_id="DF-11", origin="http://fixture.invalid", status="ok", status_code=200, environment="unit-test", validation_mode="simulation")
simulated = reconcile([], [probe])
results["simulation_probe"] = {"operation": simulated.items[0].operation.value, "claims": len(simulated.claims), "serialized_probe_retained": "unit-test" in simulated.model_dump_json()}

root = WORK / "baseline"
root.mkdir()
(root / "source.md").write_text("current content\n", encoding="utf-8")
(root / "catalog.json").write_text(json.dumps([dict(source_id="fixture", kind="source_file", relative_path="source.md", required=True)]), encoding="utf-8")
(root / "claims.json").write_text(json.dumps({"claims": [claim("stale").model_dump(mode="json")]}), encoding="utf-8")
captured = io.StringIO()
with redirect_stdout(captured):
    collect_code = baseline_main(["collect", "--root", str(root), "--catalog", str(root / "catalog.json"), "--claims", str(root / "claims.json"), "--base-sha", BASE, "--out", str(root / "out")])
    verify_stale = baseline_main(["verify", "--snapshot", str(root / "out/snapshot.json"), "--root", str(root)])
results["stale_claim_hash"] = {"collect_code": collect_code, "verify_code": verify_stale, "actual_source_hash": hashlib.sha256((root / "source.md").read_bytes()).hexdigest(), "claim_hash": HASH}
snapshot_path = root / "out/snapshot.json"
data = json.loads(snapshot_path.read_text(encoding="utf-8"))
data["hf02_readiness"] = "ready"
data["blocker_codes"] = []
data["issues"] = []
data["items"][0].update(integration="verified", operation="verified", evidence_ids=["nonexistent-claim"], dependencies=["DF-11"])
snapshot_path.write_text(json.dumps(data), encoding="utf-8")
with redirect_stdout(captured):
    forged_verify = baseline_main(["verify", "--snapshot", str(snapshot_path), "--root", str(root)])
results["forged_snapshot"] = {"verify_code": forged_verify, "forgery": "self-cycle, missing evidence ref, operation/integration verified, readiness ready"}
missing_catalog = [dict(source_id="fixture", kind="source_file", relative_path="absent.md", required=True)]
(root / "missing-catalog.json").write_text(json.dumps(missing_catalog), encoding="utf-8")
with redirect_stdout(captured):
    missing_code = baseline_main(["collect", "--root", str(root), "--catalog", str(root / "missing-catalog.json"), "--claims", str(root / "claims.json"), "--base-sha", BASE, "--out", str(root / "missing-out")])
results["missing_required_source_collect_code"] = missing_code
(WORK / "baseline_stdout.txt").write_text(captured.getvalue(), encoding="utf-8")
results["cross_prefix_dependencies"] = {"input": "HF-01 with dependency DF-11", "output": _parse_markdown_dependencies("DF-11", "HF")}
oversize = probe_endpoint(ProbeSpec(probe_id="large", item_id="DF-11", url="http://fixture.invalid/health", environment="fixture"), transport=lambda *args: ProbeResponse(200, b"a" * (64 * 1024 + 1)))
results["probe_64k_plus_1"] = {"status": oversize.status.value, "configured_default_timeout": ProbeSpec(probe_id="x", item_id="DF-11", url="http://fixture.invalid", environment="fixture").timeout_seconds}

effect_root = WORK / "approval"
effect_root.mkdir()
store = NativeEffectStore(effect_root)
store.record_approval(dict(decision_id="decision-A", workflow_id="workflow-A", release_digest="release-A", choice="approve"))
second = store.record_approval(dict(decision_id="decision-B", workflow_id="workflow-A", release_digest="release-B", choice="approve"))
results["divergent_workflow_approval"] = {"accepted": True, "second_digest": second.release_digest, "same_workflow": second.workflow_id}

adapter_root = WORK / "adapter"
adapter_root.mkdir()
config = LabConfig(lab_id="audit", root_dir=adapter_root, runtime="native_sqlite", runtime_version="native-core", workflow_version="v1", database_alias="darkfac_hf02_audit", effect_base_url="http://127.0.0.1:1")
valid_config_path = adapter_root / "valid-config.json"
valid_config_path.write_text(config.model_dump_json(), encoding="utf-8")
process_result = subprocess.run([sys.executable, "-B", "-m", "spikes.runtime_choice.driver", "--config", str(valid_config_path)], input='{"command_id":"shutdown","action":"shutdown"}\n', capture_output=True, text=True, encoding="utf-8", cwd=REPO)
results["real_driver_cli"] = {"exit_code": process_result.returncode, "stdout": process_result.stdout, "stderr": process_result.stderr, "config_path": str(valid_config_path)}
try:
    LabConfig.model_validate_json(config.model_dump_json())
    results["valid_config_json_roundtrip"] = "accepted"
except Exception as error:
    results["valid_config_json_roundtrip"] = {"exception": type(error).__name__, "errors": [{"field": list(item["loc"]), "type": item["type"]} for item in error.errors()]}
adapter = NativeAdapter(config)
try:
    value = adapter.observe(DriverCommand(command_id="observe", action="observe", workflow_id="unknown"))
    results["observe_unknown"] = [event.model_dump(mode="json") for event in value]
except Exception as error:
    results["observe_unknown"] = {"exception": type(error).__name__, "message": str(error)}
finally:
    adapter.shutdown()

blocked_root = WORK / "blocked-store"
(blocked_root / "native/orchestrator.sqlite3").mkdir(parents=True)
blocked = config.model_copy(update={"root_dir": blocked_root})
config_file = blocked_root / "config.json"
config_file.write_text(blocked.model_dump_json(), encoding="utf-8")
try:
    results["inaccessible_native_store"] = {"exit_code": driver_main(["--config", str(config_file)])}
except Exception as error:
    results["inaccessible_native_store"] = {"uncaught_exception": type(error).__name__, "message": str(error)}
try:
    NativeAdapter(blocked)
    results["inaccessible_native_store_direct"] = "accepted"
except Exception as error:
    results["inaccessible_native_store_direct"] = {"uncaught_exception": type(error).__name__, "message": str(error)}

scenario_result = ScenarioResult(lab_id="audit", scenario_id="R01", runtime="native_sqlite", repeat_index=1, status="pass", assertions={}, duration_ms=0, effect_count=0, actual_step_invocations=0)
try:
    ScenarioResult.model_validate({**scenario_result.model_dump(), "validation_mode": "real_lab", "environment_ref": "environment.json", "target_differences": []})
    results["result_environment_fields"] = "accepted"
except Exception as error:
    results["result_environment_fields"] = {"exception": type(error).__name__, "fields": sorted({str(item["loc"][0]) for item in error.errors()})}

(WORK / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
print(json.dumps({"artifact_root": str(WORK), "results": results}, indent=2, ensure_ascii=False))
