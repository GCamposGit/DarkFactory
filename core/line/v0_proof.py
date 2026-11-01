"""Prova da V0 (USR-279): 5 demandas reais seguidas sem toque do owner.

Regra (`docs/plans/PLANO_V0_2026-10-24.md`, secao 1): 5 demandas reais seguidas entram por
Telegram, Hub ou chat e chegam em producao sem o owner tocar, cada uma com learning pack; pelo
menos 1 e de um produto real (``project_id`` diferente de ``darkfac``); os tres canais funcionam.

``evaluate_v0_proof`` e pura (sem I/O, sem relogio implicito): aplica a regra sobre uma lista de
``DeliveryEvidence`` ja coletada, na ordem cronologica em que as entregas aconteceram.
``collect_v0_proof_evidence`` monta essa lista a partir do control store da linha
(``core.line.owner_intake``), do ``DemandsStore`` (o canal vem da tag ``channel:<canal>`` que
``owner_intake.submit_owner_demand`` grava no ticket), do ``OwnerActionStore`` e dos arquivos de
learning pack por ticket em ``.factory/learning_packs/`` -- nunca cria uma fonte de verdade
paralela a essas stores.
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from core.learning_pack.storage import DEFAULT_PACKS_DIR
from core.line.owner_intake import extract_run_delivery_evidence, open_line_store, run_state, ticket_external_id
from core.owner_actions.models import ActionKind
from core.paths import project_root, state_root

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
    learning_pack_linked: bool = False
    manual_page_changed: bool = False
    merge_sha: str | None = None
    smoke_passed: bool = False
    delivery_evidence: str = ""  # human-readable evidence (extract_run_delivery_evidence)
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
    manual_page_changed: bool = False


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
    manual_page_changed: bool = False
    occurred_at: str | None = None


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
    if not delivery.manual_page_changed:
        reasons.append("manual_nao_atualizado")
    if not delivery.merge_sha:
        reasons.append("sem_sha_de_merge")
    if not delivery.smoke_passed:
        reasons.append("smoke_nao_verde")
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
    the single-touch acceptance -- is tolerated), a linked learning pack, a changed
    ``docs/manual/`` page, a merge SHA and a green smoke. Any missing item disqualifies the
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
            manual_page_changed=item.manual_page_changed,
        )
        streak = streak + [evaluated] if evaluated.qualifies else []

    channels_covered = sorted({item.channel for item in streak})
    projects_involved = sorted({item.project_id for item in streak})
    has_real_product = any(p != "darkfac" for p in projects_involved)
    missing_channels = ALLOWED_CHANNELS - set(channels_covered)
    criterion_met = len(streak) >= required_streak and has_real_product and not missing_channels

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
                manual_page_changed=item.manual_page_changed,
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


def _merge_sha_from_status(status: dict[str, Any]) -> str | None:
    for job in status.get("jobs") or []:
        if job.get("stage") == "integration" and job.get("status") == "succeeded":
            refs = job.get("output_refs") or []
            if len(refs) >= 2 and isinstance(refs[1], str) and _SHA_RE.match(refs[1]):
                return refs[1]
    return None


def _build_deploy_smoke_passed(status: dict[str, Any]) -> bool:
    """Whether the build_deploy stage (core.line.stage_release) recorded a green smoke ref."""
    for job in status.get("jobs") or []:
        if job.get("stage") == "build_deploy" and job.get("status") == "succeeded":
            refs = job.get("output_refs") or []
            if any(isinstance(ref, str) and ref.startswith("smoke:") for ref in refs):
                return True
    return False


def _has_learning_pack(ticket_id: str, packs_dir: Path) -> bool:
    return any((packs_dir / f"{ticket_id}.{ext}").is_file() for ext in ("json", "md"))


def _manual_page_changed(repo_root: Path, merge_sha: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "show", "--name-only", "--pretty=format:", merge_sha],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("v0_proof: git show falhou para %s: %s", merge_sha, type(exc).__name__)
        return False
    if result.returncode != 0:
        return False
    return any(line.strip().replace("\\", "/").startswith("docs/manual/") for line in result.stdout.splitlines())


def _owner_touches(
    ticket_id: str,
    owner_actions: list[Any],
    window_start: datetime | None,
    window_end: datetime | None,
) -> tuple[int, int]:
    """(acoes, decisoes) do owner ligadas ao ticket, criadas dentro da janela do run."""
    if window_start is None:
        return 0, 0
    end = window_end or window_start
    actions = 0
    decisions = 0
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
        else:
            decisions += 1
    return actions, decisions


def _delivery_evidence(
    *,
    ticket: Any,
    line_store: Any,
    owner_actions: list[Any],
    packs_dir: Path,
    repo_root: Path,
    channel: str,
) -> DeliveryEvidence:
    notes: list[str] = []
    occurred_at = ticket.created_at.isoformat() if getattr(ticket, "created_at", None) else None
    run_id: str | None = None
    finder = getattr(line_store, "find_intake_runs", None)
    if finder is None:
        notes.append("store_sem_find_intake_runs")
    else:
        try:
            pairs = finder("owner", ticket_external_id(ticket.id))
            run_id = pairs[-1][1] if pairs else None
        except Exception:
            logger.warning("v0_proof: find_intake_runs falhou para %s", ticket.id, exc_info=True)
            notes.append("run_lookup_failed")

    if run_id is None:
        notes.append("run_nao_encontrado")
        return DeliveryEvidence(
            ticket_id=ticket.id,
            project_id=ticket.project_id,
            channel=channel,
            occurred_at=occurred_at,
            source_notes=notes,
        )

    state = run_state(line_store, run_id)
    delivery_evidence = extract_run_delivery_evidence(line_store, run_id)

    status: dict[str, Any] = {}
    getter = getattr(line_store, "get_run_status", None)
    if getter is None:
        notes.append("store_sem_get_run_status")
    else:
        try:
            status = getter(run_id) or {}
        except Exception:
            logger.warning("v0_proof: get_run_status falhou para run %s", run_id, exc_info=True)
            notes.append("run_status_unavailable")

    merge_sha = _merge_sha_from_status(status)
    window_start = _parse_dt(status.get("created_at"))
    window_end = _parse_dt(status.get("completed_at")) or _parse_dt(status.get("updated_at"))
    owner_actions_count, owner_decisions_count = _owner_touches(ticket.id, owner_actions, window_start, window_end)

    return DeliveryEvidence(
        ticket_id=ticket.id,
        project_id=ticket.project_id,
        channel=channel,
        run_id=run_id,
        in_progress=state == "in_flight",
        owner_actions_count=owner_actions_count,
        owner_decisions_count=owner_decisions_count,
        learning_pack_linked=_has_learning_pack(ticket.id, packs_dir),
        manual_page_changed=_manual_page_changed(repo_root, merge_sha) if merge_sha else False,
        merge_sha=merge_sha,
        smoke_passed=_build_deploy_smoke_passed(status),
        delivery_evidence=delivery_evidence,
        occurred_at=occurred_at,
        source_notes=notes,
    )


def collect_v0_proof_evidence(
    *,
    store: Any | None = None,
    demands_store: Any | None = None,
    owner_actions_store: Any | None = None,
    project_registry: Any | None = None,
    learning_packs_dir: Path | None = None,
    repo_root: Path | None = None,
    now: datetime | None = None,
    required_streak: int = REQUIRED_STREAK,
) -> V0ProofReport:
    """Assembles ``DeliveryEvidence`` from the line's existing stores and applies the V0 rule.

    Reuses ``open_line_store``/``run_state``/``extract_run_delivery_evidence``
    (``core.line.owner_intake``), ``DemandsStore`` (the channel comes from the
    ``channel:<telegram|hub|chat>`` tag ``submit_owner_demand`` attaches to the ticket -- there is
    no separate channel store), ``OwnerActionStore`` and the ticket-keyed learning pack files under
    ``.factory/learning_packs/``. A ticket without a ``channel:`` tag predates this instrumentation
    (or did not go through ``submit_owner_demand``) and is excluded from the candidate universe,
    not treated as a disqualifying entry.

    Never raises: an unreachable source is recorded in ``degraded_sources`` with a structured code,
    never the raw exception text (it may carry a connection string or other credential).
    """
    moment = now or datetime.now(UTC)
    degraded: list[str] = []

    line_store = store
    if line_store is None:
        try:
            line_store = open_line_store()
        except Exception:
            logger.warning("v0_proof: control store da linha indisponivel", exc_info=True)
            degraded.append("control_store_unavailable")

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

    registry = project_registry
    if registry is None:
        try:
            from core.projects.registry import get_project_registry

            registry = get_project_registry()
        except Exception:
            logger.warning("v0_proof: ProjectRegistry indisponivel", exc_info=True)
            degraded.append("project_registry_unavailable")

    packs_dir = learning_packs_dir or DEFAULT_PACKS_DIR
    repo = repo_root or project_root()

    tickets: list[Any] = []
    if demands is not None:
        try:
            tickets = sorted(demands.list_tickets(), key=lambda t: t.created_at)
        except Exception:
            logger.warning("v0_proof: leitura de tickets falhou", exc_info=True)
            degraded.append("demands_read_failed")

    deliveries: list[DeliveryEvidence] = []
    if line_store is not None:
        for ticket in tickets:
            channel = _channel_from_tags(ticket.tags)
            if not channel:
                continue
            evidence = _delivery_evidence(
                ticket=ticket,
                line_store=line_store,
                owner_actions=owner_actions,
                packs_dir=packs_dir,
                repo_root=repo,
                channel=channel,
            )
            if registry is not None:
                try:
                    if registry.get_project(ticket.project_id) is None:
                        evidence.source_notes.append(f"projeto_nao_registrado:{ticket.project_id}")
                except Exception:
                    logger.warning("v0_proof: consulta ao ProjectRegistry falhou", exc_info=True)
            deliveries.append(evidence)

    return evaluate_v0_proof(deliveries, now=moment, required_streak=required_streak, degraded_sources=degraded)


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
            lines.append(f"- `{item.ticket_id}` ({item.project_id}, canal {item.channel})")
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
