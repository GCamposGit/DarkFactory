"""Wait for a route instead of parking the run on a human (USR-87).

`pick()` returning nothing means every eligible account is rate-limited, in cooldown or at/below the
critical quota threshold. That is a condition of TIME, not a decision of the owner: the quota resets,
the cooldown ends. Before USR-87 the development and review stages answered it with
`waiting_human(no_route_available)`, and nothing ever woke such a job (`resume_blocked_job` runs only
on a human answer or a probe), so the run sat there until someone noticed.

Every agent stage (`development`, `independent_review`, `grill`, `planning`) now asks
`RouteWaiter.no_route_result` instead, which returns

* `retry("no_route_available not_before=<iso>")` while the run is still inside its wall-clock budget
  (`run_caps.wall_clock_hours`, measured from the run's `created_at` in the control store -- the
  same clock `core.workflow.successors` applies to every `not_before` retry); `<iso>` comes from
  `routing.earliest_route_available_at` (earliest cooldown end / known quota reset, else +60 min);
  the cause code stays within the 64 characters persisted for it;
* `waiting_human(no_route_available)` plus a `HumanRequest(kind="infra")` (upgrade / billing guide)
  only AFTER that budget: waiting longer than the run is allowed to live is a real infrastructure
  problem a human has to fix.

`tests/line/test_no_route_contract.py` scans `core/line/*.py` and fails if `no_route_available`
shows up next to `waiting_human` anywhere but `RouteWaiter.no_route_result`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from core.line import routing
from core.line.human import HumanRequest, request_human_help
from core.line.routing import ResetLookup, RoutingConfig
from core.projects.models import ProjectDescriptor
from core.workflow.control_contracts import StageResult

logger = logging.getLogger(__name__)

NO_ROUTE_CAUSE = "no_route_available"

# Routing stage keys (`line_routing.json`) whose job stage has another name.
_JOB_STAGE_BY_ROUTING_STAGE = {"review": "independent_review"}

# `successors.materialize_result` turns a `not_before` retry into `failed(loop_cap)` once the run is
# past its wall-clock budget. Treating the last minutes of the budget as already spent guarantees the
# retry this module returns is never converted that way between the stage's clock and the scheduler's.
WALL_CLOCK_MARGIN = timedelta(minutes=2)

RunStartLookup = Callable[[str], Optional[datetime]]
HumanRequester = Callable[[ProjectDescriptor, HumanRequest], Any]


def parse_run_created_at(raw: Any) -> Optional[datetime]:
    """Aware-UTC datetime from a store's `created_at` value (ISO string or datetime), else None."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        parsed = raw
    else:
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def run_started_at_lookup(store: Any) -> RunStartLookup:
    """`run_id -> when the run started`, from `ControlStore.get_run_created_at` (None when unknown).

    `StageContext`/`Claim` carry no run start (only the job's lease), so the run's `created_at` row in
    the control store is the clock -- exactly what `successors._run_wall_clock_exceeded` uses.
    """

    def _lookup(run_id: str) -> Optional[datetime]:
        getter = getattr(store, "get_run_created_at", None)
        if getter is None:
            return None
        try:
            return parse_run_created_at(getter(run_id))
        except Exception as exc:  # a store hiccup must not turn into a parked run
            logger.warning("Could not read the start of run %s: %s", run_id, exc)
            return None

    return _lookup


@dataclass
class RouteWaiter:
    """Decides what a stage returns when `pick()` found no route; every collaborator is injectable.

    `run_started_at` maps a run id to its start (see `run_started_at_lookup`); without it the
    wall-clock budget cannot be enforced here and the stage keeps waiting (the scheduler still
    bounds `not_before` retries by the same clock when the store knows it). `request_human` runs
    only past the budget and defaults to `core.line.human.request_human_help` (persist on the run
    branch + owner message); its failures never change the outcome.
    """

    run_started_at: Optional[RunStartLookup] = None
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    earliest: Callable[..., datetime] = routing.earliest_route_available_at
    request_human: HumanRequester = request_human_help
    cooldown_path: Optional[Path] = None
    reset_lookup: Optional[ResetLookup] = None
    margin: timedelta = field(default=WALL_CLOCK_MARGIN)

    def no_route_result(
        self,
        stage: str,
        project: ProjectDescriptor,
        run_id: str,
        *,
        host_caps: Iterable[str],
        config: Optional[RoutingConfig] = None,
        mode: Optional[str] = None,
        implementing_harness: Optional[str] = None,
        exclude: Iterable[Any] = (),
    ) -> StageResult:
        """`StageResult` for "`pick(stage, ...)` returned nothing" (`stage` is the routing stage key)."""
        cfg = config or routing.load_routing_config()
        now = self.clock()
        started = self.run_started_at(run_id) if self.run_started_at is not None else None
        cap = timedelta(hours=cfg.run_caps.wall_clock_hours)

        if started is None:
            logger.warning(
                "Run %s: start unknown, cannot enforce the %gh wall-clock budget while waiting for a route",
                run_id, cfg.run_caps.wall_clock_hours,
            )
        elif now - started >= cap - self.margin:
            return self._park_on_infra(stage, project, run_id, elapsed=now - started, cfg=cfg, mode=mode)

        until = self.earliest(
            stage,
            list(host_caps),
            cfg,
            now=now,
            mode=mode,
            implementing_harness=implementing_harness,
            exclude=exclude,
            cooldown_path=self.cooldown_path,
            reset_lookup=self.reset_lookup,
        )
        stamp = until.astimezone(timezone.utc).replace(microsecond=0).isoformat()
        logger.warning(
            "No route available for stage %s of run %s; retrying at %s instead of waiting for a human",
            stage, run_id, stamp,
        )
        return StageResult(outcome="retry", cause_code=f"{NO_ROUTE_CAUSE} not_before={stamp}")

    # -- past the run's wall-clock budget --------------------------------------------------------

    def _park_on_infra(
        self,
        stage: str,
        project: ProjectDescriptor,
        run_id: str,
        *,
        elapsed: timedelta,
        cfg: RoutingConfig,
        mode: Optional[str],
    ) -> StageResult:
        job_stage = _JOB_STAGE_BY_ROUTING_STAGE.get(stage, stage)
        request = HumanRequest(
            kind="infra",
            run_id=run_id,
            blocking_stage=job_stage,
            guide_md=infra_guide(
                job_stage,
                waited_h=elapsed.total_seconds() / 3600.0,
                cap_h=cfg.run_caps.wall_clock_hours,
                writes_code=(mode or routing.stage_mode(stage)) == "write",
            ),
        )
        logger.warning(
            "Run %s waited %.1fh for a '%s' route (budget %gh): parking it for the owner",
            run_id, elapsed.total_seconds() / 3600.0, stage, cfg.run_caps.wall_clock_hours,
        )
        try:
            self.request_human(project, request)
        except Exception as exc:  # the outcome below is what matters; a lost message is only logged
            logger.warning("Human request for run %s was not delivered: %s", run_id, exc)
        return StageResult(outcome="waiting_human", cause_code=NO_ROUTE_CAUSE)


def infra_guide(job_stage: str, *, waited_h: float, cap_h: float, writes_code: bool) -> str:
    """Step-by-step owner guide for "no AI account has quota for this stage" (ASCII on purpose)."""
    steps = [
        "No PowerShell, rode: Set-Location C:\\dev\\DarkFac; python -m core.usage.cli accounts --refresh. "
        "A lista mostra cada conta (anthropic, openai, google, xai) com a cota restante e a data de reset.",
        "Se a assinatura estiver sem cota, espere o reset mostrado ou faca upgrade do plano no painel da "
        "propria conta (Claude, ChatGPT, Google AI ou xAI).",
    ]
    if writes_code:
        steps.append(
            "Esta etapa escreve codigo e o OpenRouter e somente leitura: so uma conta de assinatura com "
            "mais de 15% de cota destrava."
        )
    else:
        steps.append(
            "Alternativa paga por uso: adicione credito ao OpenRouter em "
            "https://openrouter.ai/settings/credits (valor sugerido: US$ 10) e confirme que a variavel "
            "OPENROUTER_API_KEY esta definida no worker."
        )
    steps.append(
        "Rode o comando do passo 1 de novo e confirme que ao menos uma conta aparece com mais de 15% "
        "de cota."
    )
    steps.append(
        "Retome o run: o job fica parado em 'waiting_human' ate la, e a fabrica volta a rotear sozinha "
        "assim que houver rota."
    )
    numbered = "\n".join(f"{i}. {text}" for i, text in enumerate(steps, start=1))
    return (
        f"kind=infra: a fabrica esperou {waited_h:.1f}h (limite de relogio do run: {cap_h:g}h) por uma "
        f"conta de IA com cota livre para a etapa '{job_stage}' e nenhuma voltou. Passo a passo:\n{numbered}"
    )


__all__ = [
    "NO_ROUTE_CAUSE",
    "WALL_CLOCK_MARGIN",
    "RouteWaiter",
    "infra_guide",
    "parse_run_created_at",
    "run_started_at_lookup",
]
