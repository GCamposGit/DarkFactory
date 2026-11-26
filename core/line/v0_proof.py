"""Prova da V0 (USR-279): 5 demandas reais seguidas sem toque do owner.

Regra (`docs/plans/PLANO_V0_2026-10-24.md`, secao 1): 5 demandas reais seguidas entram por
Telegram, Hub ou chat e chegam em producao sem o owner tocar, cada uma com learning pack; pelo
menos 1 e de um produto real (``project_id`` diferente de ``darkfac``); os tres canais funcionam.

``evaluate_v0_proof`` e pura (sem I/O, sem relogio implicito): aplica a regra sobre uma lista de
``DeliveryEvidence`` ja coletada, na ordem cronologica em que as entregas aconteceram.
``collect_v0_proof_evidence`` monta essa lista a partir do que o motor unico (``run_ticket.py``)
realmente emite: o recibo ``merge.json`` e o ``deploy_report.json`` do ticket
(``core.line.ticket_engine.artifacts_root``), o learning pack em ``.factory/learning_packs/`` e a
presenca do ``merge_sha`` na branch padrao do projeto no registro (``ProjectDescriptor``). O canal
vem da tag ``channel:<canal>`` do ``DemandsStore``; as acoes do owner, do ``OwnerActionStore``.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from core.learning_pack.storage import DEFAULT_PACKS_DIR
from core.owner_actions.models import ActionKind
from core.paths import state_root

logger = logging.getLogger(__name__)

ALLOWED_CHANNELS = frozenset({"telegram", "hub", "chat"})
REQUIRED_STREAK = 5
# Grill never creates an OwnerAction (it is answered via grill_answers/Telegram inline keyboard,
# see docs/manual/fila.md); the single owner decision an M1-style delivery is allowed is the
# one-touch acceptance (e.g. approving a post before it is published).
MAX_OWNER_DECISIONS_PER_DELIVERY = 1
CHANNEL_TAG_PREFIX = "channel:"
REPORTS_DIR = state_root() / "reports" / "v0-proof"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class DeliveryEvidence(BaseModel):
    """Raw facts the collector gathers for one real demand, before the rule is applied."""

    ticket_id: str
    project_id: str
    channel: str = ""  # "telegram" | "hub" | "chat"; "" when the source could not be determined
    run_id: str | None = None
    in_progress: bool = False
    owner_actions_count: int = 0  # ActionKind.ACTION tied to the ticket during the run (never allowed)
    owner_decisions_count: int = 0  # ActionKind.DECISION tied to the ticket during the run (<=1: the aceite)
    intervention_types: list[str] = Field(default_factory=list)
    learning_pack_linked: bool = False
    merge_sha: str | None = None
    merge_on_default_branch: bool = False  # merge_sha reachable from the project's default branch
    smoke_passed: bool = False
    delivery_evidence: str = ""  # ticket.delivery_evidence (PR url or commit)
    occurred_at: str | None = None
    source_notes: list[str] = Field(default_factory=list)  # degraded facts about THIS delivery


class QualifiedDelivery(BaseModel):
    """One delivery already judged against the V0 rule."""

    ticket_id: str
    project_id: str
    channel: str
    run_id: str | None
    occurred_at: str | None = None
    qualifies: bool
    reasons: list[str] = Field(default_factory=list)
    # Evidence carried through for display (e.g. the DarkHub /live V0-proof panel, USR-279 T3):
    # never used by `evaluate_v0_proof` itself to decide `qualifies`, only to show what backs it.
    merge_sha: str | None = None
    delivery_evidence: str = ""
    smoke_passed: bool = False
    learning_pack_linked: bool = False
    owner_interventions_count: int = 0
    owner_intervention_types: list[str] = Field(default_factory=list)
    learning_pack_ref: str | None = None


class V0ProofSlot(BaseModel):
    """One of the `REQUIRED_STREAK` fixed slots shown on the DarkHub /live V0-proof panel.

    Slots are ordered oldest-first, same as `V0ProofReport.qualified_deliveries`; `filled=False`
    marks a slot the factory still needs to deliver (streak shorter than the required size).
    """

    index: int = Field(ge=1)
    filled: bool
    ticket_id: str | None = None
    project_id: str | None = None
    channel: str | None = None
    run_id: str | None = None
    merge_sha: str | None = None
    delivery_evidence: str = ""
    smoke_passed: bool = False
    learning_pack_linked: bool = False
    occurred_at: str | None = None
    owner_interventions_count: int = 0
    owner_intervention_types: list[str] = Field(default_factory=list)
    learning_pack_ref: str | None = None


class V0ProofReport(BaseModel):
    generated_at: str
    current_streak: int
    qualified_deliveries: list[QualifiedDelivery]
    channels_covered: list[str]
    projects_involved: list[str]
    criterion_met: bool
    missing: list[str] = Field(default_factory=list)
    degraded_sources: list[str] = Field(default_factory=list)
    slots: list[V0ProofSlot] = Field(default_factory=list)


def _disqualify_reasons(delivery: DeliveryEvidence) -> list[str]:
    reasons: list[str] = []
    if delivery.in_progress:
        reasons.append("entrega_em_andamento")
    if delivery.channel not in ALLOWED_CHANNELS:
        reasons.append(f"canal_invalido:{delivery.channel or 'desconhecido'}")
    if delivery.owner_actions_count > 0:
        reasons.append(f"acao_do_owner_fora_do_grill_e_aceite:{delivery.owner_actions_count}")
    if delivery.owner_decisions_count > MAX_OWNER_DECISIONS_PER_DELIVERY:
        reasons.append(f"mais_de_uma_decisao_do_owner:{delivery.owner_decisions_count}")
    if not delivery.learning_pack_linked:
        reasons.append("learning_pack_ausente")
    if not delivery.merge_sha:
        reasons.append("sem_sha_de_merge")
    elif not delivery.merge_on_default_branch:
        reasons.append("merge_fora_da_branch_padrao")
    if not delivery.smoke_passed:
        reasons.append("smoke_nao_verde")
    if any(code in {"owner_actions_store_unavailable", "owner_actions_read_failed"} for code in delivery.source_notes):
        reasons.append("intervencoes_owner_indisponiveis")
    return reasons


def evaluate_v0_proof(
    deliveries: list[DeliveryEvidence],
    *,
    now: datetime,
    required_streak: int = REQUIRED_STREAK,
    degraded_sources: list[str] | None = None,
) -> V0ProofReport:
    """Pure: no I/O, no implicit clock (``now`` is always given by the caller).

    ``deliveries`` must already be in chronological order (oldest first). A delivery qualifies
    with a channel in ``{telegram, hub, chat}``, no owner action created during the run outside
    grill and aceite (grill itself never creates an ``OwnerAction``; at most one owner decision --
    the single-touch acceptance -- is tolerated), a linked learning pack, a merge SHA
    reachable from the project's default branch and a green smoke. Any missing item disqualifies the
    delivery and resets the running streak to zero; an in-progress delivery never counts as green.
    ``criterion_met`` requires the trailing streak to reach ``required_streak``, cover the three
    channels, and include at least one delivery whose ``project_id`` is not ``darkfac``.
    """
    streak: list[QualifiedDelivery] = []
    for item in deliveries:
        reasons = _disqualify_reasons(item)
        evaluated = QualifiedDelivery(
            ticket_id=item.ticket_id,
            project_id=item.project_id,
            channel=item.channel,
            run_id=item.run_id,
            occurred_at=item.occurred_at,
            qualifies=not reasons,
            reasons=reasons,
            merge_sha=item.merge_sha,
            delivery_evidence=item.delivery_evidence,
            smoke_passed=item.smoke_passed,
            learning_pack_linked=item.learning_pack_linked,
            owner_interventions_count=item.owner_actions_count + item.owner_decisions_count,
            owner_intervention_types=item.intervention_types,
            learning_pack_ref=f".factory/learning_packs/{item.ticket_id}" if item.learning_pack_linked else None,
        )
        streak = streak + [evaluated] if evaluated.qualifies else []

    channels_covered = sorted({item.channel for item in streak})
    projects_involved = sorted({item.project_id for item in streak})
    has_real_product = any(p != "darkfac" for p in projects_involved)
    missing_channels = ALLOWED_CHANNELS - set(channels_covered)
    intervention_source_ok = not any(
        code in {"owner_actions_store_unavailable", "owner_actions_read_failed"}
        for code in (degraded_sources or [])
    )
    criterion_met = len(streak) >= required_streak and has_real_product and not missing_channels and intervention_source_ok

    missing: list[str] = []
    if len(streak) < required_streak:
        missing.append(
            f"faltam {required_streak - len(streak)} entrega(s) qualificada(s) seguida(s) "
            f"(streak atual {len(streak)}/{required_streak})"
        )
    if not has_real_product:
        missing.append("falta ao menos 1 entrega qualificada de um produto real (project_id != darkfac)")
    if missing_channels:
        missing.append(f"falta(m) canal(is) na sequencia qualificada: {', '.join(sorted(missing_channels))}")
    if not intervention_source_ok:
        missing.append("nao foi possivel comprovar a contagem de intervencoes humanas")

    return V0ProofReport(
        generated_at=now.isoformat(),
        current_streak=len(streak),
        qualified_deliveries=streak,
        channels_covered=channels_covered,
        projects_involved=projects_involved,
        criterion_met=criterion_met,
        missing=missing,
        degraded_sources=list(degraded_sources or []),
        slots=build_v0_proof_slots(streak, slot_count=required_streak),
    )


def build_v0_proof_slots(
    streak: list[QualifiedDelivery], *, slot_count: int = REQUIRED_STREAK
) -> list[V0ProofSlot]:
    """Projects the trailing qualified streak onto exactly `slot_count` fixed slots.

    `streak` must already be oldest-first (same order as `V0ProofReport.qualified_deliveries`).
    Slot 1 is the oldest of the last `slot_count` qualified deliveries, slot `slot_count` the most
    recent; leading empty slots (`filled=False`) mark demands the factory still needs to deliver
    for the DarkHub /live panel to show the sequence as complete.
    """
    recent = streak[-slot_count:]
    pad = slot_count - len(recent)
    slots = [V0ProofSlot(index=i + 1, filled=False) for i in range(pad)]
    for offset, item in enumerate(recent):
        slots.append(
            V0ProofSlot(
                index=pad + offset + 1,
                filled=True,
                ticket_id=item.ticket_id,
                project_id=item.project_id,
                channel=item.channel,
                run_id=item.run_id,
                merge_sha=item.merge_sha,
                delivery_evidence=item.delivery_evidence,
                smoke_passed=item.smoke_passed,
                learning_pack_linked=item.learning_pack_linked,
                owner_interventions_count=item.owner_interventions_count,
                owner_intervention_types=item.owner_intervention_types,
                learning_pack_ref=item.learning_pack_ref,
                occurred_at=item.occurred_at,
            )
        )
    return slots


# ---------------------------------------------------------------------------
# Collector: assembles DeliveryEvidence from the stores the line already writes to
# ---------------------------------------------------------------------------


def _channel_from_tags(tags: list[str]) -> str:
    for tag in tags:
        if tag.startswith(CHANNEL_TAG_PREFIX):
            return tag[len(CHANNEL_TAG_PREFIX) :].strip()
    return ""


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _run_artifacts(ticket_id: str, artifacts_dir: Path) -> Path:
    """`.darkfac/runs/<ticket>/` of the single engine (`core.line.ticket_engine.run_workspace`)."""
    return artifacts_dir / ticket_id / ".darkfac" / "runs" / ticket_id


def _merge_receipt(run_dir: Path) -> tuple[str | None, datetime | None]:
    """(merge_sha, end of delivery) from `merge.json`; the end is the latest of merge/deploy receipts."""
    path = run_dir / "merge.json"
    sha = _read_json(path).get("merge_sha")
    if not isinstance(sha, str) or not _SHA_RE.match(sha.strip()):
        return None, None
    mtimes: list[float] = []
    for receipt in (path, run_dir / "deploy_report.json"):
        try:
            mtimes.append(receipt.stat().st_mtime)
        except OSError:
            continue  # receipt cleared concurrently: that delivery simply does not count now
    if not mtimes:
        return None, None
    return sha.strip(), datetime.fromtimestamp(max(mtimes), UTC)


def _smoke_passed(run_dir: Path, merge_sha: str) -> bool:
    """`deploy_report.json` of the same sha with a green smoke."""
    report = _read_json(run_dir / "deploy_report.json")
    return report.get("sha") == merge_sha and report.get("smoke_ok") is True


def _has_learning_pack(ticket_id: str, packs_dir: Path) -> bool:
    names = (ticket_id, f"lp-{ticket_id.lower()}")  # the store names a pack `lp-<id>` (ticket_extras)
    return any((packs_dir / f"{n}.{ext}").is_file() for n in names for ext in ("json", "md"))


def _merge_on_default_branch(merge_sha: str, repo: Path | None, default_branch: str) -> bool:
    """True when ``merge_sha`` is an ancestor of ``origin/<default_branch>`` (never a local branch)."""
    if repo is None or not repo.is_dir():
        return False
    try:
        # Best effort: a failed fetch just leaves the last known remote ref.
        subprocess.run(["git", "fetch", "-q", "origin", default_branch], cwd=repo, capture_output=True, timeout=60)
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", merge_sha, f"refs/remotes/origin/{default_branch}"],
            cwd=repo, capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("v0_proof: git merge-base falhou para %s: %s", merge_sha, type(exc).__name__)
        return False
    return result.returncode == 0


def _owner_touches(
    ticket_id: str,
    owner_actions: list[Any],
    window_start: datetime | None,
    window_end: datetime | None,
) -> tuple[int, int, list[str]]:
    """(acoes, decisoes, tipos) do owner dentro da janela do run."""
    if window_start is None:
        return 0, 0, []
    end = window_end or window_start
    actions = 0
    decisions = 0
    types: list[str] = []
    for action in owner_actions:
        if ticket_id not in action.blocks and ticket_id not in action.depends_on:
            continue
        created = action.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if not (window_start <= created <= end):
            continue
        if action.kind is ActionKind.ACTION:
            actions += 1
            types.append("action")
        else:
            decisions += 1
            types.append("decision")
    return actions, decisions, types


def _delivery_evidence(
    *,
    ticket: Any,
    project: Any,
    owner_actions: list[Any],
    packs_dir: Path,
    artifacts_dir: Path,
    channel: str,
) -> DeliveryEvidence:
    """Facts of one ticket read from what run_ticket emits: merge/deploy receipts and learning pack."""
    run_dir = _run_artifacts(ticket.id, artifacts_dir)
    merge_sha, merged_at = _merge_receipt(run_dir)
    created = ticket.created_at if getattr(ticket, "created_at", None) else None
    notes: list[str] = []
    if project is None:
        notes.append(f"projeto_nao_registrado:{ticket.project_id}")
    on_default = False
    if merge_sha:
        # Unregistered project: no repo to prove against, never fall back to DarkFac's own.
        if project is not None:
            on_default = _merge_on_default_branch(merge_sha, project.resolve_path(), project.default_branch)
    actions, decisions, kinds = _owner_touches(ticket.id, owner_actions, _parse_dt(created), merged_at)
    when = merged_at or _parse_dt(created)
    return DeliveryEvidence(
        ticket_id=ticket.id,
        project_id=ticket.project_id,
        channel=channel,
        run_id=ticket.id if merge_sha else None,
        in_progress=str(getattr(ticket.status, "value", ticket.status)) == "in_progress",
        owner_actions_count=actions,
        owner_decisions_count=decisions,
        intervention_types=kinds,
        learning_pack_linked=_has_learning_pack(ticket.id, packs_dir),
        merge_sha=merge_sha,
        merge_on_default_branch=on_default,
        smoke_passed=_smoke_passed(run_dir, merge_sha) if merge_sha else False,
        delivery_evidence=str(getattr(ticket, "delivery_evidence", None) or ""),
        occurred_at=when.isoformat() if when else None,
        source_notes=notes,
    )


def collect_v0_proof_evidence(
    *,
    store: Any | None = None,  # ignorado: o motor unico nao grava mais o DAG da linha (mantido p/ chamadores)
    demands_store: Any | None = None,
    owner_actions_store: Any | None = None,
    project_registry: Any | None = None,
    learning_packs_dir: Path | None = None,
    repo_root: Path | None = None,  # ignorado: cada projeto usa o proprio repositorio do registro
    artifacts_dir: Path | None = None,
    now: datetime | None = None,
    required_streak: int = REQUIRED_STREAK,
) -> V0ProofReport:
    """Assembles ``DeliveryEvidence`` from what the single engine emits and applies the V0 rule.

    Per ticket: ``merge.json``/``deploy_report.json`` under ``core.line.ticket_engine.artifacts_root``,
    the ticket-keyed learning pack under ``.factory/learning_packs/`` and the ``merge_sha`` reachable
    from ``origin/<default_branch>`` of the project in the registry (external repos included; no commit in
    DarkFac's ``docs/manual/`` is required). The channel comes from the ``channel:<telegram|hub|chat>``
    tag ``submit_owner_demand`` attaches to the ticket; a ticket without it predates this
    instrumentation and is excluded from the candidate universe, not treated as disqualified.

    Never raises: an unreachable source is recorded in ``degraded_sources`` with a structured code,
    never the raw exception text (it may carry a connection string or other credential).
    """
    moment = now or datetime.now(UTC)
    degraded: list[str] = []

    demands = demands_store
    if demands is None:
        try:
            from core.demands.store import DemandsStore

            demands = DemandsStore()
        except Exception:
            logger.warning("v0_proof: DemandsStore indisponivel", exc_info=True)
            degraded.append("demands_store_unavailable")

    actions_store = owner_actions_store
    if actions_store is None:
        try:
            from core.owner_actions.store import OwnerActionStore

            actions_store = OwnerActionStore()
        except Exception:
            logger.warning("v0_proof: OwnerActionStore indisponivel", exc_info=True)
            degraded.append("owner_actions_store_unavailable")

    owner_actions: list[Any] = []
    if actions_store is not None:
        try:
            owner_actions = actions_store.list_actions()
        except Exception:
            logger.warning("v0_proof: leitura do backlog de acoes do owner falhou", exc_info=True)
            degraded.append("owner_actions_read_failed")
    else:
        degraded.append("owner_actions_store_unavailable")

    registry = project_registry
    if registry is None:
        try:
            from core.projects.registry import get_project_registry

            registry = get_project_registry()
        except Exception:
            logger.warning("v0_proof: ProjectRegistry indisponivel", exc_info=True)
            degraded.append("project_registry_unavailable")

    if artifacts_dir is None:
        from core.line.ticket_engine import artifacts_root

        artifacts_dir = artifacts_root()

    tickets: list[Any] = []
    if demands is not None:
        try:
            tickets = sorted(demands.list_tickets(), key=lambda t: t.created_at)
        except Exception:
            logger.warning("v0_proof: leitura de tickets falhou", exc_info=True)
            degraded.append("demands_read_failed")

    deliveries: list[DeliveryEvidence] = []
    for ticket in tickets:
        channel = _channel_from_tags(ticket.tags)
        if not channel:
            continue
        project = None
        if registry is not None:
            try:
                project = registry.get_project(ticket.project_id)
            except Exception:
                logger.warning("v0_proof: consulta ao ProjectRegistry falhou", exc_info=True)
        deliveries.append(
            _delivery_evidence(
                ticket=ticket,
                project=project,
                owner_actions=owner_actions,
                packs_dir=learning_packs_dir or DEFAULT_PACKS_DIR,
                artifacts_dir=artifacts_dir,
                channel=channel,
            )
        )

    deliveries.sort(key=lambda item: (_parse_dt(item.occurred_at) or datetime.max.replace(tzinfo=UTC), item.ticket_id))
    return evaluate_v0_proof(
        deliveries, now=moment, required_streak=required_streak, degraded_sources=sorted(set(degraded))
    )


def save_v0_proof_report(report: V0ProofReport, out_dir: Path | None = None, *, day: date | None = None) -> Path:
    """Writes the report to ``<out_dir>/<day>.json``. Idempotent: identical input -> identical bytes."""
    target_dir = out_dir or REPORTS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    report_day = day or datetime.fromisoformat(report.generated_at).date()
    path = target_dir / f"{report_day.isoformat()}.json"
    payload = report.model_dump_json(indent=2)
    if path.is_file() and path.read_text(encoding="utf-8") == payload:
        return path
    path.write_text(payload, encoding="utf-8")
    return path


def render_v0_proof_markdown(report: V0ProofReport) -> str:
    """Renders ``report`` as the Markdown read by the owner and by `docs/manual/prova_v0.md`.

    Every line is a statement of fact or of what the factory does next -- never a question: the CLI
    never asks the owner anything (what is missing is phrased as the factory's own next action, see
    ``evaluate_v0_proof``'s ``missing`` entries).
    """
    lines = [
        "# Prova da V0 (USR-279)",
        "",
        f"**Gerado em**: {report.generated_at}  ",
        f"**Status**: {'CUMPRIDO' if report.criterion_met else 'EM ANDAMENTO'}  ",
        f"**Streak qualificado**: {report.current_streak}/{REQUIRED_STREAK}  ",
        f"**Canais cobertos na sequencia**: {', '.join(report.channels_covered) or 'nenhum'}  ",
        f"**Projetos na sequencia**: {', '.join(report.projects_involved) or 'nenhum'}  ",
        "",
        "---",
        "",
        "## Entregas qualificadas na sequencia atual",
        "",
    ]
    if report.qualified_deliveries:
        for item in report.qualified_deliveries:
            lines.append(
                f"- `{item.ticket_id}` ({item.project_id}, canal {item.channel}, run {item.run_id or 'indisponivel'}, "
                f"SHA {item.merge_sha or 'indisponivel'}, learning pack "
                f"{item.learning_pack_ref or 'ausente'}, intervencoes humanas "
                f"{item.owner_interventions_count} [{', '.join(item.owner_intervention_types) or 'nenhuma'}])"
            )
    else:
        lines.append("Nenhuma entrega qualificada na sequencia atual.")
    lines.append("")

    if report.missing:
        lines.append("## O que a fabrica ainda precisa entregar")
        lines.append("")
        for item in report.missing:
            lines.append(f"- {item}")
        lines.append("")

    if report.degraded_sources:
        lines.append("## Fontes indisponiveis nesta leitura")
        lines.append("")
        for source in report.degraded_sources:
            lines.append(f"- `{source}`")
        lines.append("")

    return "\n".join(lines)


def save_v0_proof_markdown(report: V0ProofReport, out_dir: Path | None = None, *, day: date | None = None) -> Path:
    """Writes the report to ``<out_dir>/<day>.md``. Idempotent: identical input -> identical bytes."""
    target_dir = out_dir or REPORTS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    report_day = day or datetime.fromisoformat(report.generated_at).date()
    path = target_dir / f"{report_day.isoformat()}.md"
    content = render_v0_proof_markdown(report)
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return path
    path.write_text(content, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="DarkFac: prova da V0 (USR-279)")
    parser.add_argument("--out-dir", type=Path, default=None, help="Diretorio de saida dos relatorios")
    parser.add_argument(
        "--report",
        action="store_true",
        help=(
            "Grava json e md em .factory/reports/v0-proof/ e sai 1 enquanto o criterio nao for "
            "cumprido (sem --report, so imprime o resumo e sai 0)"
        ),
    )
    args = parser.parse_args(argv)

    report = collect_v0_proof_evidence()
    print(
        f"Prova da V0: streak={report.current_streak}/{REQUIRED_STREAK} "
        f"criterion_met={report.criterion_met} canais={report.channels_covered} "
        f"projetos={report.projects_involved}"
    )
    for item in report.missing:
        print(f"  falta: {item}")
    for source in report.degraded_sources:
        print(f"  fonte indisponivel: {source}")

    if not args.report:
        return 0

    json_path = save_v0_proof_report(report, args.out_dir)
    md_path = save_v0_proof_markdown(report, args.out_dir)
    print(f"Relatorios gravados em:\n  - {json_path}\n  - {md_path}")
    return 0 if report.criterion_met else 1


if __name__ == "__main__":
    sys.exit(main())
