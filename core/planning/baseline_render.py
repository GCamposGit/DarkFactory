"""Stable Markdown and JSON projections for HF-01 baseline evidence."""

from __future__ import annotations

from typing import Any

from .baseline_models import BaselineSnapshot, CollectedBaseline, EvidenceClaim


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_dump(item) for item in value]
    if isinstance(value, tuple):
        return [_dump(item) for item in value]
    return value


def render_sources(collected: CollectedBaseline, claims: list[EvidenceClaim] | tuple[EvidenceClaim, ...], snapshot: BaselineSnapshot) -> dict[str, Any]:
    """Return a sanitized machine projection without source payloads."""

    return {
        "schema_version": "1",
        "snapshot_id": snapshot.snapshot_id,
        "source_observations": _dump(snapshot.source_observations),
        "planned_items": _dump(collected.planned_items),
        "claims": _dump(list(claims)),
        "issues": _dump(snapshot.issues),
    }


def render_baseline(snapshot: BaselineSnapshot) -> str:
    """Render the audit view while keeping evidence dimensions separate."""

    lines = [
        "# HF-01 baseline",
        "",
        f"- Snapshot: `{snapshot.snapshot_id}`",
        f"- Observed at: `{snapshot.observed_at.isoformat()}`",
        f"- Base SHA: `{snapshot.base_sha}`",
        f"- Source fingerprint: `{snapshot.source_fingerprint}`",
        f"- Completeness: **{snapshot.completeness.value}**",
        f"- HF-02 readiness: **{snapshot.hf02_readiness.value}**",
        "",
        "## Source observations",
        "",
        "| Source | Path | Status | SHA-256 | Error |",
        "| --- | --- | --- | --- | --- |",
    ]
    for observation in snapshot.source_observations:
        lines.append(f"| {observation.source_id} | `{observation.relative_path.as_posix()}` | {observation.status.value} | `{observation.sha256 or '—'}` | {observation.error_code or '—'} |")
    lines.extend(["", "## Capability assessments", "", "| Item | Declared | Implementation | Integration | Operation | Dependencies |", "| --- | --- | --- | --- | --- | --- |"])
    for item in snapshot.items:
        lines.append(f"| {item.item_id} | {item.declared_status} | {item.implementation.value} | {item.integration.value} | {item.operation.value} | {', '.join(item.dependencies) or '—'} |")
    lines.extend(["", "## Issues", ""])
    if snapshot.issues:
        lines.extend(["| Code | Severity | Items | Sources | Required action |", "| --- | --- | --- | --- | --- |"])
        for issue in snapshot.issues:
            lines.append(f"| {issue.code} | {issue.severity.value} | {', '.join(issue.item_ids) or '—'} | {', '.join(issue.source_ids) or '—'} | {issue.required_action} |")
    else:
        lines.append("Nenhuma pendência foi registrada.")
    lines.extend([
        "",
        "## Evidence policy",
        "",
        "A leitura de uma fonte não promove automaticamente implementação, integração ou operação.",
        "Simulação, declaração documental e probe no ambiente-alvo permanecem identificados separadamente.",
        "",
        "## Next action",
        "",
        "A evidência mínima do runtime/store/DF-11 está disponível para iniciar o spike HF-02." if snapshot.hf02_readiness.value == "ready" else "Completar as pendências de runtime/store/DF-11 antes de iniciar o spike HF-02.",
    ])
    return "\n".join(lines) + "\n"


__all__ = ["render_baseline", "render_sources"]
