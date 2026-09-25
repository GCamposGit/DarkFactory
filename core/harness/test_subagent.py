"""
Specialized Test Subagent Engine for DarkFac.

Executes test suites headless, isolates raw logs to disk, and produces
cognitively dense, distilled test reports (DistilledTestReport) without
polluting agent context with hundreds of lines of output.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("core.harness.test_subagent")


class TestScope(StrEnum):
    __test__ = False
    ALL = "all"
    QUICK = "quick"
    FILE = "file"
    PATTERN = "pattern"
    FAILED_ONLY = "failed_only"


class HarnessType(StrEnum):
    __test__ = False
    ANTIGRAVITY = "antigravity"
    CODEX = "codex"
    GROK = "grok"
    CLAUDE_CODE = "claude_code"


class ExecutionTier(StrEnum):
    __test__ = False
    LOCAL_ZERO_COST = "local_zero_cost"
    LIGHT_HARNESS = "light_harness"
    DEEP_DIAGNOSTIC = "deep_diagnostic"


class HarnessRunnerSpec(BaseModel):
    """Specification of optimal model, reasoning effort, and invocation for a harness."""
    __test__ = False
    harness: HarnessType = Field(..., description="Target harness platform")
    model_id: str = Field(..., description="Recommended model identifier")
    reasoning_effort: str = Field(default="none", description="Reasoning effort level: none, low, medium, high, max")
    execution_tier: ExecutionTier = Field(..., description="Execution tier: local, light, or deep")
    rationale: str = Field(..., description="Technical rationale for model and effort selection")
    config_snippet: str = Field(default="", description="Configuration or invocation snippet for the harness")


def get_harness_test_runner_spec(
    harness: HarnessType | str,
    prefer_local: bool = False,
    deep_rca: bool = False,
) -> HarnessRunnerSpec:
    """Determine the optimal model and reasoning effort for a specific harness."""
    norm_harness = HarnessType(str(harness).lower())

    if prefer_local and not deep_rca:
        return HarnessRunnerSpec(
            harness=norm_harness,
            model_id="ollama/qwen-code-fast:latest",
            reasoning_effort="none",
            execution_tier=ExecutionTier.LOCAL_ZERO_COST,
            rationale="Zero-cost local worker ($0) for rapid TDD loops and syntax/unit test verification.",
            config_snippet="ollama run qwen-code-fast:latest (endpoint: http://localhost:11434)",
        )

    if norm_harness == HarnessType.ANTIGRAVITY:
        model = "flash_lite" if not deep_rca else "pro"
        return HarnessRunnerSpec(
            harness=norm_harness,
            model_id=model,
            reasoning_effort="low" if not deep_rca else "high",
            execution_tier=ExecutionTier.LIGHT_HARNESS if not deep_rca else ExecutionTier.DEEP_DIAGNOSTIC,
            rationale="Antigravity native subagent using flash_lite for high-throughput zero-clutter execution.",
            config_snippet='invoke_subagent(TypeName="self", Role="Test Runner", Model="flash_lite")',
        )

    elif norm_harness == HarnessType.CODEX:
        if deep_rca:
            return HarnessRunnerSpec(
                harness=norm_harness,
                model_id="openai/gpt-5-6-luna-high",
                reasoning_effort="max",
                execution_tier=ExecutionTier.DEEP_DIAGNOSTIC,
                rationale="Luna max: High-depth reasoning for complex test failure diagnosis, concurrency bugs, and architectural edge cases.",
                config_snippet='{"model": "openai/gpt-5-6-luna-high", "reasoning_effort": "max"}',
            )
        else:
            return HarnessRunnerSpec(
                harness=norm_harness,
                model_id="openai/gpt-5-6-luna-high",
                reasoning_effort="low",
                execution_tier=ExecutionTier.LIGHT_HARNESS,
                rationale="Luna low: Ultra-fast execution and syntactic failure parsing without excessive reasoning overhead (40% lower cost, ~0.45s latency).",
                config_snippet='{"model": "openai/gpt-5-6-luna-high", "reasoning_effort": "low"}',
            )

    elif norm_harness == HarnessType.GROK:
        effort = "low" if not deep_rca else "high"
        return HarnessRunnerSpec(
            harness=norm_harness,
            model_id="xai/grok-4.6",
            reasoning_effort=effort,
            execution_tier=ExecutionTier.LIGHT_HARNESS if not deep_rca else ExecutionTier.DEEP_DIAGNOSTIC,
            rationale="Grok 4.6 with reasoning_effort='low' for rapid local terminal verification and parallel Arena subagent execution.",
            config_snippet='grok build --reasoning-effort low --exec "python core/harness/test_subagent.py"',
        )

    elif norm_harness == HarnessType.CLAUDE_CODE:
        if deep_rca:
            return HarnessRunnerSpec(
                harness=norm_harness,
                model_id="anthropic/claude-3.7-sonnet",
                reasoning_effort="medium",
                execution_tier=ExecutionTier.DEEP_DIAGNOSTIC,
                rationale="Claude 3.7 Sonnet with controlled thinking for architectural debugging and complex test regressions.",
                config_snippet='claude --model claude-3-7-sonnet --agent test-runner',
            )
        else:
            return HarnessRunnerSpec(
                harness=norm_harness,
                model_id="anthropic/claude-3.5-haiku",
                reasoning_effort="none",
                execution_tier=ExecutionTier.LIGHT_HARNESS,
                rationale="Claude 3.5 Haiku: Subagent standard for lightweight, ultra-low latency command execution and output distillation.",
                config_snippet='claude --model claude-3-5-haiku-latest (subagent in .claude/agents/test-runner.md)',
            )

    raise ValueError(f"Unknown harness: {harness}")


class TestExecutionInstruction(BaseModel):
    """Specification of tests to run, timeouts, and options for the subagent."""
    __test__ = False
    target: Optional[str] = Field(default=None, description="Test target path, file, or pattern")
    scope: TestScope = Field(default=TestScope.FILE, description="Scope of test execution")
    timeout_seconds: int = Field(default=60, description="Max execution duration in seconds")
    fail_fast: bool = Field(default=False, description="Stop immediately on first failure (-x)")
    extra_args: List[str] = Field(default_factory=list, description="Additional pytest flags")
    # 'local' by default: the worker's /execute runs pytest in the WORKER's own
    # checkout, not the caller's edits, so auto-offloading gave results for the
    # wrong code. Full-suite offload goes through core/harness/runner.py, which
    # ships the candidate commit as a git bundle.
    worker_mode: str = Field(default="local", description="Worker execution mode: 'local', 'auto', or 'remote' (remote runs the worker's own checkout)")
    remote_worker_url: Optional[str] = Field(default="http://100.78.181.90:8080", description="URL of the on-premise dedicated test worker")
    allow_fallback: bool = Field(default=True, description="Fallback to local execution if remote worker is unreachable or errors")
    worker_probe_timeout: float = Field(default=1.0, description="Healthcheck probe timeout in seconds")


class FailedTestItem(BaseModel):
    """Clean, structured representation of an isolated test failure."""
    test_id: str = Field(..., description="Canonical test identifier (e.g. tests/test_foo.py::test_bar)")
    file: str = Field(..., description="Path to the file containing the test or failure")
    line: Optional[int] = Field(default=None, description="Line number of failure")
    error_type: str = Field(default="AssertionError", description="Exception or failure type")
    error_message: str = Field(default="", description="Isolated error message")
    snippet: str = Field(default="", description="Concise code and assertion failure snippet")


class DistilledTestReport(BaseModel):
    """Distilled test execution report designed for minimal context consumption."""
    verdict: str = Field(..., description="PASSED, FAILED, ERROR, or TIMEOUT")
    success: bool = Field(..., description="True if all tests passed and exit code is 0")
    exit_code: int = Field(..., description="Subprocess return code")
    duration_seconds: float = Field(..., description="Duration in seconds")
    total_discovered: int = Field(default=0, description="Total number of tests discovered")
    passed_count: int = Field(default=0, description="Number of passed tests")
    failed_count: int = Field(default=0, description="Number of failed tests")
    skipped_count: int = Field(default=0, description="Number of skipped tests")
    failures: List[FailedTestItem] = Field(default_factory=list, description="List of isolated failures")
    concise_summary: str = Field(..., description="One-line summary of results")
    agent_feedback: str = Field(..., description="Direct, actionable feedback pointing to file and line")
    raw_log_path: Optional[str] = Field(default=None, description="Path to isolated raw log on disk")
    worker_id: str = Field(default="local", description="Identifier of worker node executing tests")
    execution_mode: str = Field(default="local", description="Actual execution mode: local, remote, or local_fallback")


def generate_subagent_prompt(
    instruction: TestExecutionInstruction,
    harness: HarnessType | str = HarnessType.ANTIGRAVITY,
    prefer_local: bool = False,
    deep_rca: bool = False,
) -> str:
    """Generate instructions for a specialized subagent runner."""
    spec = get_harness_test_runner_spec(harness, prefer_local=prefer_local, deep_rca=deep_rca)
    fail_fast_text = "-x (fail_fast enabled: stop immediately on first failure)" if instruction.fail_fast else "run full suite"
    return (
        f"You are a Specialized Test Runner subagent for DarkFactory.\n"
        f"Harness Environment: {spec.harness.value.upper()} (Model: {spec.model_id}, Effort: {spec.reasoning_effort}).\n"
        f"Goal: Execute test target '{instruction.target or 'suite'}' under scope '{instruction.scope}'.\n"
        f"Timeout: {instruction.timeout_seconds} seconds.\n"
        f"Options: {fail_fast_text}.\n\n"
        f"Instructions:\n"
        f"1. Run tests deterministically using headless pytest execution.\n"
        f"2. Isolate raw logs to .factory/test_logs/ and parse output into DistilledTestReport.\n"
        f"3. Return ONLY the DistilledTestReport structure. Never dump hundreds of verbose log lines.\n"
        f"4. Configuration hint: {spec.config_snippet}"
    )


class TestSubagentEngine:
    """Headless engine to execute tests and parse outputs into distilled reports."""
    __test__ = False

    def __init__(
        self,
        log_dir: Optional[Path] = None,
        project_root: Optional[Path] = None,
    ) -> None:
        self.project_root = project_root or Path(__file__).resolve().parents[2]
        self.log_dir = log_dir or (self.project_root / ".factory" / "test_logs")
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Could not create test log directory %s: %s", self.log_dir, exc)

    def parse_output(
        self,
        output: str,
        exit_code: int,
        duration_seconds: float,
    ) -> DistilledTestReport:
        """Parse raw pytest stdout/stderr into a clean DistilledTestReport."""
        passed_count = 0
        failed_count = 0
        skipped_count = 0

        # Match summary line (e.g. "==== 15 passed, 1 skipped in 1.45s ====")
        passed_m = re.search(r"(\d+)\s+passed", output)
        if passed_m:
            passed_count = int(passed_m.group(1))

        failed_m = re.search(r"(\d+)\s+failed", output)
        if failed_m:
            failed_count = int(failed_m.group(1))

        skipped_m = re.search(r"(\d+)\s+skipped", output)
        if skipped_m:
            skipped_count = int(skipped_m.group(1))

        # Check for errors in collection or session
        errors_m = re.search(r"(\d+)\s+error", output)
        errors_count = int(errors_m.group(1)) if errors_m else 0

        total_discovered = passed_count + failed_count + skipped_count + errors_count

        # Parse short test summary info if present
        short_summary_failures: Dict[str, Dict[str, str]] = {}
        short_summary_pattern = re.compile(r"^FAILED\s+([^\s]+)\s+-\s+(?:(\w+):\s+)?(.*)$", re.MULTILINE)
        for m in short_summary_pattern.finditer(output):
            tid = m.group(1).strip()
            etype = m.group(2) or "AssertionError"
            emsg = m.group(3).strip()
            short_summary_failures[tid] = {
                "error_type": etype,
                "error_message": emsg,
            }

        # Parse detailed FAILURES section
        failures: List[FailedTestItem] = []
        failures_section_match = re.search(
            r"=+ FAILURES =+\s*\n(.*?)(?:\n=+ (?:short test summary info|.* in [\d.]+s) =+)",
            output,
            re.DOTALL,
        )
        failures_text = failures_section_match.group(1) if failures_section_match else ""

        if failures_text:
            # Each failure in FAILURES begins with "___ test_name ___"
            raw_blocks = re.split(r"\n_{3,}\s*(.*?)\s*_{3,}\n", "\n" + failures_text)
            # raw_blocks will alternate: [preamble, test_header_1, block_1, test_header_2, block_2, ...]
            idx = 1
            while idx < len(raw_blocks):
                header = raw_blocks[idx].strip()
                body = raw_blocks[idx + 1] if idx + 1 < len(raw_blocks) else ""
                idx += 2

                # Extract code snippet with > and E
                snippet_lines: List[str] = []
                extracted_error_type = "AssertionError"
                extracted_error_msg = ""
                for line in body.splitlines():
                    stripped = line.strip()
                    if line.startswith(">") or line.startswith("E "):
                        snippet_lines.append(line)
                    if line.startswith("E "):
                        # e.g. "E   AssertionError: assert 20 == 25"
                        e_content = line[2:].strip()
                        if ":" in e_content:
                            parts = e_content.split(":", 1)
                            extracted_error_type = parts[0].strip()
                            extracted_error_msg = parts[1].strip()
                        else:
                            extracted_error_msg = e_content

                snippet = "\n".join(snippet_lines).strip()

                # Extract file and line from e.g. "tests\test_demo.py:84: AssertionError"
                file_line_m = re.search(r"^([^\s:]+(?:\.py))(?::(\d+))(?::\s*(\w+))?", body, re.MULTILINE)
                file_path = ""
                line_no: Optional[int] = None
                if file_line_m:
                    file_path = file_line_m.group(1).strip()
                    line_no = int(file_line_m.group(2))
                    if file_line_m.group(3):
                        extracted_error_type = file_line_m.group(3).strip()

                # Determine matching test_id from short summary or header
                test_id = header
                for tid, s_info in short_summary_failures.items():
                    if header in tid:
                        test_id = tid
                        if not extracted_error_msg and s_info["error_message"]:
                            extracted_error_msg = s_info["error_message"]
                        if s_info["error_type"]:
                            extracted_error_type = s_info["error_type"]
                        break

                if not file_path:
                    # fallback to test_id path
                    if "::" in test_id:
                        file_path = test_id.split("::")[0]
                    else:
                        file_path = test_id

                failures.append(
                    FailedTestItem(
                        test_id=test_id,
                        file=file_path,
                        line=line_no,
                        error_type=extracted_error_type,
                        error_message=extracted_error_msg,
                        snippet=snippet,
                    )
                )

        # If short summary found failures but detailed section was empty
        if not failures and short_summary_failures:
            for tid, info in short_summary_failures.items():
                f_path = tid.split("::")[0] if "::" in tid else tid
                failures.append(
                    FailedTestItem(
                        test_id=tid,
                        file=f_path,
                        line=None,
                        error_type=info["error_type"],
                        error_message=info["error_message"],
                        snippet="",
                    )
                )

        success = exit_code == 0 and failed_count == 0 and errors_count == 0
        verdict = "PASSED" if success else ("ERROR" if errors_count > 0 and failed_count == 0 else "FAILED")

        # Build concise summary
        parts: List[str] = []
        if failed_count > 0:
            parts.append(f"{failed_count} failed")
        if passed_count > 0:
            parts.append(f"{passed_count} passed")
        if skipped_count > 0:
            parts.append(f"{skipped_count} skipped")
        if errors_count > 0:
            parts.append(f"{errors_count} errors")
        if not parts:
            parts.append(f"Exit code {exit_code}")
        concise_summary = f"{', '.join(parts)} in {duration_seconds:.2f}s"

        # Build distilled agent feedback
        if success:
            agent_feedback = "All tests passed successfully."
        else:
            feedback_items: List[str] = []
            for f in failures:
                loc = f"{f.file}:{f.line}" if f.line else f.file
                msg = f": {f.error_message}" if f.error_message else ""
                feedback_items.append(f"{loc} [{f.error_type}]{msg}")
            if feedback_items:
                agent_feedback = "Test failures detected:\n- " + "\n- ".join(feedback_items)
            else:
                agent_feedback = f"Tests failed with exit code {exit_code}. Summary: {concise_summary}"

        return DistilledTestReport(
            verdict=verdict,
            success=success,
            exit_code=exit_code,
            duration_seconds=duration_seconds,
            total_discovered=total_discovered,
            passed_count=passed_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            failures=failures,
            concise_summary=concise_summary,
            agent_feedback=agent_feedback,
        )

    def probe_remote_worker(self, worker_url: str, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
        """Probe the remote test worker's /health endpoint with a strict short timeout."""
        health_url = f"{worker_url.rstrip('/')}/health"
        try:
            req = urllib.request.Request(health_url, headers={"User-Agent": "DarkFac-TestEngine/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    raw = resp.read().decode("utf-8")
                    data = json.loads(raw)
                    return data
        except Exception as exc:
            logger.debug("Probe failed for %s: %s", health_url, exc)
            return None
        return None

    def execute_remote(self, instruction: TestExecutionInstruction) -> DistilledTestReport:
        """Offload test execution to the remote worker daemon via HTTP POST /execute."""
        if not instruction.remote_worker_url:
            raise ValueError("remote_worker_url must be provided for remote test execution.")

        execute_url = f"{instruction.remote_worker_url.rstrip('/')}/execute"
        # Serialize instruction (forcing worker_mode='local' remotely to prevent infinite forward recursion)
        req_payload = instruction.model_dump()
        req_payload["worker_mode"] = "local"
        data_bytes = json.dumps(req_payload).encode("utf-8")

        req = urllib.request.Request(
            execute_url,
            data=data_bytes,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "DarkFac-TestEngine/1.0",
            },
            method="POST",
        )

        # Allow extra buffer over instruction.timeout_seconds for network overhead
        http_timeout = max(5.0, float(instruction.timeout_seconds) + 10.0)
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Remote worker returned HTTP status {resp.status}")
            resp_bytes = resp.read()
            report_dict = json.loads(resp_bytes.decode("utf-8"))
            return DistilledTestReport.model_validate(report_dict)

    def execute_local(
        self,
        instruction: TestExecutionInstruction,
        execution_mode: str = "local",
    ) -> DistilledTestReport:
        """Execute pytest locally according to instructions and return a DistilledTestReport."""
        cmd = [sys.executable, "-m", "pytest"]

        if instruction.scope == TestScope.QUICK:
            cmd.extend(["-m", "not slow"])
        elif instruction.scope == TestScope.FAILED_ONLY:
            cmd.append("--lf")

        if instruction.fail_fast:
            cmd.append("-x")

        if instruction.target:
            cmd.append(instruction.target)

        if instruction.extra_args:
            cmd.extend(instruction.extra_args)

        start_time = datetime.datetime.now(datetime.timezone.utc)
        run_id = f"{int(start_time.timestamp())}_{uuid.uuid4().hex[:8]}"
        log_file_path = self.log_dir / f"test_run_{run_id}.log"

        logger.info("Running tests locally: %s (Timeout: %ds)", " ".join(cmd), instruction.timeout_seconds)

        subp_kwargs: Dict[str, Any] = {}
        if sys.platform == "win32":
            subp_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(self.project_root),
                timeout=instruction.timeout_seconds,
                encoding="utf-8",
                errors="replace",
                **subp_kwargs,
            )
            raw_stdout = res.stdout or ""
            raw_stderr = res.stderr or ""
            combined_output = raw_stdout
            if raw_stderr:
                combined_output += "\n--- STDERR ---\n" + raw_stderr

            exit_code = res.returncode
        except subprocess.TimeoutExpired as exc:
            raw_stdout = exc.stdout or "" if isinstance(exc.stdout, str) else ""
            raw_stderr = exc.stderr or "" if isinstance(exc.stderr, str) else ""
            combined_output = raw_stdout + "\n[TIMEOUT EXPIRED]\n" + raw_stderr
            exit_code = 124
        except Exception as exc:
            combined_output = f"[EXECUTION ERROR]: {exc}"
            exit_code = 2

        duration = (datetime.datetime.now(datetime.timezone.utc) - start_time).total_seconds()

        # Isolate full verbose log to disk
        try:
            log_file_path.write_text(combined_output, encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to write log file %s: %s", log_file_path, exc)

        report = self.parse_output(combined_output, exit_code=exit_code, duration_seconds=duration)
        report.raw_log_path = str(log_file_path.resolve())
        report.worker_id = "local"
        report.execution_mode = execution_mode

        return report

    def execute(self, instruction: TestExecutionInstruction) -> DistilledTestReport:
        """
        Execute tests with automatic worker dispatch and failover.
        Supports 'local', 'remote', and 'auto' modes.
        """
        target_mode = (instruction.worker_mode or "auto").lower()

        if target_mode == "local":
            return self.execute_local(instruction, execution_mode="local")

        # Auto or Remote dispatch
        worker_url = instruction.remote_worker_url
        worker_healthy = False

        if worker_url:
            health_info = self.probe_remote_worker(worker_url, timeout=instruction.worker_probe_timeout)
            if health_info and health_info.get("status") in ("ok", "healthy"):
                worker_healthy = True

        if worker_healthy:
            try:
                logger.info("Offloading test execution to remote worker at %s", worker_url)
                return self.execute_remote(instruction)
            except Exception as exc:
                logger.warning("Remote worker execution failed: %s", exc)
                if not instruction.allow_fallback or target_mode == "remote":
                    raise

        # If remote was strictly requested and failover is disallowed
        if target_mode == "remote" and not instruction.allow_fallback:
            raise RuntimeError(f"Remote test worker at {worker_url} is unavailable or failed.")

        # Fallback to local
        fallback_mode = "local_fallback" if target_mode != "local" else "local"
        logger.info("Executing tests locally (mode: %s)...", fallback_mode)
        return self.execute_local(instruction, execution_mode=fallback_mode)


def main() -> None:
    """CLI entrypoint for standalone test subagent execution."""
    parser = argparse.ArgumentParser(description="Specialized Test Subagent Engine for DarkFac")
    parser.add_argument("--target", type=str, default=None, help="Target test file, folder, or pattern")
    parser.add_argument("--scope", type=str, default="file", choices=["all", "quick", "file", "pattern", "failed_only"])
    parser.add_argument("--timeout", type=int, default=60, help="Timeout in seconds")
    parser.add_argument("-x", "--fail-fast", action="store_true", help="Fail fast on first error")
    parser.add_argument("--worker-mode", type=str, default="local", choices=["auto", "remote", "local"], help="Worker execution mode (remote runs the worker's own checkout, not local edits)")
    parser.add_argument("--worker-url", type=str, default="http://100.78.181.90:8080", help="Remote worker daemon URL")
    parser.add_argument("--no-fallback", action="store_false", dest="allow_fallback", default=True, help="Disable local fallback on remote worker failure")
    parser.add_argument("--json", action="store_true", help="Output full JSON DistilledTestReport")
    args = parser.parse_args()

    instruction = TestExecutionInstruction(
        target=args.target,
        scope=TestScope(args.scope),
        timeout_seconds=args.timeout,
        fail_fast=args.fail_fast,
        worker_mode=args.worker_mode,
        remote_worker_url=args.worker_url,
        allow_fallback=args.allow_fallback,
    )

    engine = TestSubagentEngine()
    report = engine.execute(instruction)

    if args.json:
        print(json.dumps(report.model_dump(), indent=2))
    else:
        status_marker = "[PASS]" if report.success else "[FAIL]"
        print(f"{status_marker} {report.verdict} [{report.worker_id} / {report.execution_mode}]: {report.concise_summary}")
        print(f"Log: {report.raw_log_path}")
        print(f"Agent Feedback:\n{report.agent_feedback}")

    sys.exit(0 if report.success else 1)


if __name__ == "__main__":
    main()
