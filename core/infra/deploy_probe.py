"""Availability evidence collected across a blue-green deployment (USR-207 T2)."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Optional

from core.infra.availability_probe import DEFAULT_URL, http_probe_response
from core.paths import state_root

logger = logging.getLogger(__name__)
SAMPLE_INTERVAL_SECONDS = 1.0
PROBE_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class ProbeSample:
    timestamp: float
    status: Optional[int]
    git_sha: Optional[str]
    error: Optional[str] = None


@dataclass(frozen=True)
class DeployProbeResult:
    passed: bool
    sample_count: int
    non200: int
    observed_shas: tuple[str, ...]
    deployments_for_sha: int
    duplicate_deployment: bool
    samples: tuple[ProbeSample, ...]
    jsonl_path: Path
    summary_path: Path


def default_output_dir() -> Path:
    return state_root() / "reports" / "deploy"


def alert_non200(result: DeployProbeResult, send: Optional[Callable[[str], bool]] = None) -> None:
    """Best-effort ops alert when the deployment sampling observed any non-200 response."""
    if result.non200 <= 0:
        return
    if send is None:
        from core.infra.availability_probe import default_sender

        send = default_sender
    try:
        send(
            "[DARKHUB DEPLOY] sonda observou "
            f"{result.non200} resposta(s) diferente de 200 em {result.sample_count} amostras; "
            f"shas={','.join(result.observed_shas) or 'nenhum'}. "
            f"Evidencia: {result.summary_path}"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("deploy probe ops alert failed: %s", type(exc).__name__)


def evaluate_samples(
    samples: Iterable[ProbeSample], *, target_sha: str, deployments_for_sha: int = 0
) -> DeployProbeResult:
    rows = tuple(samples)
    observed = tuple(sorted({row.git_sha.lower() for row in rows if row.git_sha}))
    non200 = sum(1 for row in rows if row.status != 200)
    passed = non200 == 0 and target_sha.lower() in observed and len(observed) >= 2
    return DeployProbeResult(
        passed=passed,
        sample_count=len(rows),
        non200=non200,
        observed_shas=observed,
        deployments_for_sha=max(0, int(deployments_for_sha)),
        duplicate_deployment=deployments_for_sha > 1,
        samples=rows,
        jsonl_path=Path(),
        summary_path=Path(),
    )


class DeployProbe:
    """Threaded one-second /health sampler. Probe and persistence failures are contained."""

    def __init__(
        self,
        *,
        target_sha: str,
        output_dir: Optional[Path] = None,
        url: str = DEFAULT_URL,
        timeout: float = PROBE_TIMEOUT_SECONDS,
        http_fn: Callable[[str, float], tuple[Optional[int], Optional[str], Optional[str]]]
        | Callable[[str, float], tuple[int, Optional[str]]]
        | None = None,
        clock_fn: Callable[[], float] = time.time,
        sleep_fn: Callable[[float], None] = time.sleep,
        run_id: Optional[str] = None,
    ) -> None:
        self.target_sha = target_sha.lower()
        self.output_dir = Path(output_dir) if output_dir else default_output_dir()
        self.url = url
        self.timeout = timeout
        self.http_fn = http_fn or http_probe_response
        self.clock_fn = clock_fn
        self.sleep_fn = sleep_fn
        self.run_id = run_id or f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
        self.samples: list[ProbeSample] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._finished: Optional[DeployProbeResult] = None

    def sample_once(self) -> ProbeSample:
        try:
            response = self.http_fn(self.url, self.timeout)
            if len(response) == 2:
                status, sha = response
                error = None
            else:
                status, sha, error = response
            sample = ProbeSample(self.clock_fn(), int(status) if status is not None else None,
                                 str(sha).lower() if sha else None, error)
        except Exception as exc:  # noqa: BLE001 - the sampler must never interrupt deploy flow
            name = "timeout" if isinstance(exc, TimeoutError) else "connection_error"
            try:
                timestamp = self.clock_fn()
            except Exception:  # noqa: BLE001
                timestamp = time.time()
            sample = ProbeSample(timestamp, None, None, name)
        self.samples.append(sample)
        return sample

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()

        def run() -> None:
            while not self._stop.is_set():
                self.sample_once()
                if self._stop.is_set():
                    break
                try:
                    self.sleep_fn(SAMPLE_INTERVAL_SECONDS)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("deploy probe sleep failed: %s", type(exc).__name__)
                    break

        self._thread = threading.Thread(target=run, name="darkhub-deploy-probe", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(5.0, self.timeout + 1.0))
            self._thread = None

    def finish(self, *, deployments_for_sha: int = 0) -> DeployProbeResult:
        if self._finished is not None:
            return self._finished
        self.stop()
        evaluated = evaluate_samples(
            self.samples, target_sha=self.target_sha, deployments_for_sha=deployments_for_sha
        )
        jsonl_path = self.output_dir / f"{self.run_id}.jsonl"
        summary_path = self.output_dir / f"{self.run_id}.json"
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
                for sample in self.samples:
                    handle.write(json.dumps(asdict(sample), sort_keys=True) + "\n")
            summary = {
                "run_id": self.run_id,
                "target_sha": self.target_sha,
                "passed": evaluated.passed,
                "sample_count": evaluated.sample_count,
                "non200": evaluated.non200,
                "observed_shas": list(evaluated.observed_shas),
                "deployments_for_sha": evaluated.deployments_for_sha,
                "duplicate_deployment": evaluated.duplicate_deployment,
                "samples_jsonl": jsonl_path.name,
            }
            summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - evidence failure must not mask deploy/rollback errors
            logger.warning("deploy probe evidence write failed: %s", type(exc).__name__)
        self._finished = replace(evaluated, jsonl_path=jsonl_path, summary_path=summary_path)
        return self._finished


__all__ = ["DeployProbe", "DeployProbeResult", "ProbeSample", "alert_non200", "default_output_dir", "evaluate_samples"]
