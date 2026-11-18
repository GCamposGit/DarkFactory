"""Windows Task Scheduler contract for the persistent fleet dispatcher.

The minute trigger is intentional.  Some Windows Task Scheduler versions do
not apply ``RestartOnFailure`` after the dispatcher exits with code 15.  The
periodic trigger provides deterministic recovery while ``IgnoreNew`` prevents
a second dispatcher from being started when the previous instance is healthy.

Queue, ticket and candidate SHA are deliberately not encoded in the task.
They remain in ``STATE_ROOT`` and are consumed by ``fleet_bootstrap.py`` on
every new process.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath
from typing import Sequence
from xml.etree import ElementTree as ET

TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
DEFAULT_TASK_NAME = "DarkFac Fleet Dispatcher"
DEFAULT_RECOVERY_INTERVAL = "PT1M"
DEFAULT_RESTART_COUNT = 999
DEFAULT_START_BOUNDARY = "2000-01-01T00:00:00Z"

ET.register_namespace("", TASK_NAMESPACE)


def _tag(name: str) -> str:
    return f"{{{TASK_NAMESPACE}}}{name}"


def _text(parent: ET.Element, name: str, value: object) -> ET.Element:
    child = ET.SubElement(parent, _tag(name))
    child.text = str(value)
    return child


def _ps_quote(value: str | os.PathLike[str]) -> str:
    """Quote one literal for a PowerShell single-quoted string."""

    return "'" + os.fspath(value).replace("'", "''") + "'"


def _task_path(value: str | os.PathLike[str], field: str) -> PurePath:
    """Resolve native paths cleanly while allowing Windows paths when generating XML on POSIX."""
    raw = os.fspath(value)
    expanded = Path(raw).expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    win = PureWindowsPath(raw)
    if win.is_absolute():
        return win
    raise ValueError(f"{field} must be an absolute path")


@dataclass(frozen=True)
class FleetTaskSpec:
    project_root: PurePath
    state_root: PurePath
    python_executable: PurePath
    bootstrap: PurePath
    task_name: str = DEFAULT_TASK_NAME
    recovery_interval: str = DEFAULT_RECOVERY_INTERVAL
    restart_count: int = DEFAULT_RESTART_COUNT
    start_boundary: str = DEFAULT_START_BOUNDARY
    powershell_executable: str = (
        r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
    )

    @classmethod
    def create(
        cls,
        project_root: str | os.PathLike[str],
        state_root: str | os.PathLike[str],
        python_executable: str | os.PathLike[str] | None = None,
        bootstrap: str | os.PathLike[str] | None = None,
        **kwargs: object,
    ) -> "FleetTaskSpec":
        project = _task_path(project_root, "project_root")
        state = _task_path(state_root, "state_root")
        python = _task_path(python_executable or sys.executable, "python_executable")
        bootstrap_path = _task_path(
            bootstrap or project / "core" / "line" / "fleet_bootstrap.py",
            "bootstrap",
        )
        if isinstance(project, Path) and not project.is_dir():
            raise ValueError(f"project_root must be an existing directory: {project}")
        if isinstance(state, Path) and not state.is_dir():
            raise ValueError(f"state_root must be an existing directory: {state}")
        if isinstance(bootstrap_path, Path) and not bootstrap_path.is_file():
            raise ValueError(f"bootstrap must be an existing file: {bootstrap_path}")
        return cls(
            project_root=project,
            state_root=state,
            python_executable=python,
            bootstrap=bootstrap_path,
            **kwargs,
        )


def bootstrap_powershell(spec: FleetTaskSpec) -> str:
    """Return the explicit, harness-independent dispatcher bootstrap.

    Environment values are assigned inside the scheduled process rather than
    inherited from the interactive installer.  In particular, no
    ``DARKFAC_OPERATING_HARNESS`` value is copied into the service.
    """

    return "; ".join(
        (
            "$ErrorActionPreference = 'Stop'",
            "$env:DARKFAC_OPERATING_HARNESS = ''",
            f"$env:PROJECT_ROOT = {_ps_quote(spec.project_root)}",
            f"$env:STATE_ROOT = {_ps_quote(spec.state_root)}",
            f"$env:PYTHONPATH = {_ps_quote(spec.project_root)}",
            f"Set-Location -LiteralPath {_ps_quote(spec.project_root)}",
            (
                f"& {_ps_quote(spec.python_executable)} "
                f"{_ps_quote(spec.bootstrap)} "
                f"--project-root {_ps_quote(spec.project_root)} "
                f"--state-root {_ps_quote(spec.state_root)} --loop"
            ),
            "exit $LASTEXITCODE",
        )
    )


def render_fleet_task_xml(spec: FleetTaskSpec) -> str:
    task = ET.Element(_tag("Task"), {"version": "1.4"})

    registration = ET.SubElement(task, _tag("RegistrationInfo"))
    _text(
        registration,
        "Description",
        (
            "DarkFac persistent fleet dispatcher. Boot and PT1M triggers are "
            "protected by IgnoreNew; state is retained in STATE_ROOT."
        ),
    )

    triggers = ET.SubElement(task, _tag("Triggers"))

    boot = ET.SubElement(triggers, _tag("BootTrigger"))
    _text(boot, "Enabled", "true")

    periodic = ET.SubElement(triggers, _tag("TimeTrigger"))
    repetition = ET.SubElement(periodic, _tag("Repetition"))
    _text(repetition, "Interval", spec.recovery_interval)
    _text(repetition, "Duration", "PT0S")
    _text(repetition, "StopAtDurationEnd", "false")
    _text(periodic, "StartBoundary", spec.start_boundary)
    _text(periodic, "Enabled", "true")

    principals = ET.SubElement(task, _tag("Principals"))
    principal = ET.SubElement(principals, _tag("Principal"), {"id": "Author"})
    _text(principal, "LogonType", "InteractiveToken")
    _text(principal, "RunLevel", "LeastPrivilege")

    settings = ET.SubElement(task, _tag("Settings"))
    _text(settings, "MultipleInstancesPolicy", "IgnoreNew")
    _text(settings, "DisallowStartIfOnBatteries", "false")
    _text(settings, "StopIfGoingOnBatteries", "false")
    _text(settings, "AllowHardTerminate", "true")
    _text(settings, "StartWhenAvailable", "true")
    _text(settings, "RunOnlyIfNetworkAvailable", "false")
    _text(settings, "AllowStartOnDemand", "true")
    _text(settings, "Enabled", "true")
    _text(settings, "Hidden", "false")
    _text(settings, "ExecutionTimeLimit", "PT0S")

    restart = ET.SubElement(settings, _tag("RestartOnFailure"))
    _text(restart, "Interval", spec.recovery_interval)
    _text(restart, "Count", spec.restart_count)

    actions = ET.SubElement(task, _tag("Actions"), {"Context": "Author"})
    execute = ET.SubElement(actions, _tag("Exec"))
    _text(execute, "Command", spec.powershell_executable)
    _text(
        execute,
        "Arguments",
        "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass "
        f"-Command {bootstrap_powershell(spec)}",
    )
    _text(execute, "WorkingDirectory", spec.project_root)

    ET.indent(task, space="  ")
    return ET.tostring(task, encoding="unicode", xml_declaration=True)


def build_fleet_task_xml(
    project_root: str | os.PathLike[str],
    state_root: str | os.PathLike[str],
    python_executable: str | os.PathLike[str] | None = None,
    bootstrap: str | os.PathLike[str] | None = None,
    **kwargs: object,
) -> str:
    """Compatibility entry point used by installers and tests."""

    spec = FleetTaskSpec.create(
        project_root=project_root,
        state_root=state_root,
        python_executable=python_executable,
        bootstrap=bootstrap,
        **kwargs,
    )
    return render_fleet_task_xml(spec)


# Compatibility aliases for older callers.
build_task_xml = build_fleet_task_xml
task_xml = build_fleet_task_xml


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--python", dest="python_executable", default=sys.executable)
    parser.add_argument("--bootstrap")
    parser.add_argument("--task-name", default=DEFAULT_TASK_NAME)
    parser.add_argument("--output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    xml = build_fleet_task_xml(
        project_root=args.project_root,
        state_root=args.state_root,
        python_executable=args.python_executable,
        bootstrap=args.bootstrap,
        task_name=args.task_name,
    )
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(xml, encoding="utf-8")
    else:
        sys.stdout.write(xml)
        if not xml.endswith("\n"):
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
