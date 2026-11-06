"""Assemble recovery proof from canonical artifacts after actual deployment."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.line.workspace import RunWorkspace


def record_implementation(ws: RunWorkspace, harness: str, model: str | None) -> None:
    """Accumulate writers across slices instead of remembering only the last one."""
    from core.line import workspace
    path = workspace.context_dir(ws) / "implementation_families.json"
    families = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    family = route_family(harness, model)
    workspace.write_context(ws, path.name, json.dumps(sorted(set(families + [family]))))


def review_capabilities(ws: RunWorkspace, caps: list[str] | tuple[str, ...]) -> list[str]:
    from core.line import workspace
    path = workspace.context_dir(ws) / "implementation_families.json"
    families = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    known = {"harness:claude": "anthropic", "harness:codex": "openai", "harness:grok": "xai", "harness:antigravity": "google"}
    return [cap for cap in caps if known.get(cap) not in families]


def independent_route(ws: RunWorkspace, harness: str, model: str | None, fallback_model: str | None = None) -> tuple[str, str | None]:
    from core.line import workspace
    from core.models.registry import resolve
    path = workspace.context_dir(ws) / "implementation_families.json"
    families = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if route_family(harness, model) in families:
        if harness != "openrouter":
            raise ValueError("review_same_implementation_family")
        if not fallback_model:
            raise ValueError("review_no_independent_paid_family")
        model = resolve(fallback_model)
        if route_family(harness, model) in families:
            raise ValueError("review_no_independent_paid_family")
    return harness, model


def route_family(harness: str, model: str | None) -> str:
    families = {"claude": "anthropic", "codex": "openai", "grok": "xai", "antigravity": "google"}
    if harness in families:
        return families[harness]
    from core.models.registry import get_registry
    for entry in get_registry().models:
        if model and model in (entry.id, *entry.aliases, *entry.gateway_ids):
            return entry.provider
    raise ValueError("review_execution_family_unknown")


def review_context(ws: RunWorkspace) -> str:
    """Paid text adapters receive actual task inputs, not inaccessible file paths."""
    from core.line import workspace
    directory = workspace.context_dir(ws)
    parts = []
    for name in ("DEMAND.md", "GRILL.md", "SPEC.md", "REVIEW_CONTEXT.md"):
        path = directory / name
        if path.is_file():
            parts.append(f"\n--- TASK CONTEXT: {name} ---\n{path.read_text(encoding='utf-8')}")
    return "\n".join(parts)


def build_receipt(ticket_id: str, run_id: str, repo_root: Path) -> dict[str, Any]:
    """Refuse absent proof; never infer deployed status from a process exit code."""
    from core.git.autonomy import _run_gh, _run_git
    from core.git.ci_checks import query_checks
    from core.learning_pack.storage import LearningPackStore
    from core.line import ticket_engine
    from core.infra.node_sync import VPS_URL
    import urllib.request

    directory = ticket_engine.artifacts_root() / ticket_id / ".darkfac" / "runs" / ticket_id

    def read(name: str) -> dict[str, Any]:
        return json.loads((directory / name).read_text(encoding="utf-8"))

    validation = read("validation.json")
    digest = validation.get("candidate_digest")
    review = read("review_execution.json")
    last = read("review_state.json")["rounds"][-1]
    if (not digest or validation.get("passed") is not True
            or last.get("candidate_digest") != digest or last.get("verdict") != "approve"
            or last.get("forced") or review.get("candidate_digest") != digest
            or not review.get("implementer_family") or not review.get("reviewer_family")
            or review["reviewer_family"] in review.get("implementation_families", [review["implementer_family"]])):
        raise ValueError("recovery_candidate_or_review_unproven")
    merge = read("merge.json")
    if merge.get("candidate_digest") != digest:
        raise ValueError("recovery_merge_candidate_mismatch")
    sha, pr_url = merge["merge_sha"], merge["pr_url"]
    reachable = _run_git(["merge-base", "--is-ancestor", sha, "origin/main"], cwd=repo_root)
    if reachable.returncode or query_checks(int(pr_url.rstrip("/").split("/")[-1]), repo_root, _run_gh).state != "green":
        raise ValueError("recovery_merge_or_ci_unproven")
    with urllib.request.urlopen(VPS_URL, timeout=20) as response:
        status, health = response.status, json.load(response)
    if not 200 <= status < 300 or health.get("status") != "ok" or health.get("git_sha") != sha:
        raise ValueError("recovery_deployed_sha_unproven")
    packs = LearningPackStore()
    pack_id = f"lp-{ticket_id.lower()}"
    if not any(row.get("pack_id") == pack_id for row in packs.list_packs()) or packs.load_pack(pack_id) is None:
        raise ValueError("recovery_learning_pack_not_indexed")
    if packs.load_pack(pack_id).metrics.get("candidate_digest") != digest:
        raise ValueError("recovery_learning_pack_candidate_mismatch")
    ws = ticket_engine.run_workspace(type("Ticket", (), {"id": ticket_id, "project_id": "darkfac"})())
    ticket_engine.write_deploy(ws, sha, True, True, "Canonical deploy and exact SHA HTTP health proof")
    stamp = datetime.now(timezone.utc).isoformat()

    def evidence(ref: str, **values) -> dict[str, Any]:
        local = not ref.startswith(("http://", "https://"))
        observed = datetime.fromtimestamp(Path(ref).stat().st_mtime, timezone.utc).isoformat() if local and Path(ref).is_file() else stamp
        return dict(candidate_digest=digest, observed_at=values.pop("observed_at", observed), evidence_ref=ref, **values)

    return {
        "schema_version": 1, "simulated": False, "run_id": run_id,
        "candidate_digest": digest, "implementer_family": review["implementer_family"],
        "gate": evidence(str(directory / "validation.json"), passed=True, marker="[HARNESS_PASS]"),
        "review": evidence(str(directory / "review_execution.json"), approved=True, independent=True,
                           implementer_family=review["implementer_family"], reviewer_family=review["reviewer_family"]),
        "ci": evidence(pr_url, green=True),
        "merge": evidence(str(directory / "merge.json"), sha=sha, reachable=True),
        "deploy": evidence(str(directory / "deploy_report.json"), sha=sha, deployed=True, observed_at=stamp),
        "smoke": evidence(VPS_URL, http_status=status),
        "learning_pack": evidence(str(packs.index_file), indexed=True, observed_at=packs.load_pack(pack_id).timestamp),
    }


def record_review_execution(ws: RunWorkspace, digest: str, implementer: str, reviewer: str, model: str | None) -> None:
    """Persist actual routing evidence alongside the bound review verdict."""
    from core.line import workspace
    try:
        path = workspace.context_dir(ws) / "implementation_families.json"
        families = json.loads(path.read_text(encoding="utf-8")) if path.exists() else [route_family(implementer, None)]
        execution = {"candidate_digest": digest,
                     "implementer_family": route_family(implementer, None),
                     "reviewer_family": route_family(reviewer, model), "implementation_families": families}
    except ValueError:
        execution = {"candidate_digest": digest, "family_evidence_missing": True}
    workspace.write_context(ws, "review_execution.json", json.dumps(execution))
