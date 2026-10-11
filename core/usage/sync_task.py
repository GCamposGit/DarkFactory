"""Pure decision logic for the ``DarkFac-Usage-Cloud-Sync`` scheduled task (USR-236).

Incident (2026-10-10): the Windows task that runs ``scripts/sync_usage_to_cloud.py``
for the quota meter pointed for seven days at a stale Codex worktree
(``...\\.codex\\worktrees\\usr-124-postmerge``). It ran outdated code in
SYNC_DEGRADED and nobody noticed. This module is the testable core of the fix:

* :func:`desired_definition` -- the task definition the host must have; it
  refuses any repository root (or interpreter) located inside a worktree.
* :func:`parse_task_xml` / :func:`interpret_query` -- read what
  ``schtasks /Query /TN ... /XML`` returned.
* :func:`plan` -- decide ``create`` / ``keep`` / ``update`` with human-readable reasons.
* :func:`build_task_xml` -- the XML registered with ``schtasks /Create /XML``.

Everything here is stdlib-only and side-effect free: it never runs ``schtasks`` and
never touches the filesystem, so it is exercised on Linux and Windows alike. The
thin CLI that actually talks to Task Scheduler is ``scripts/install_usage_sync_task.py``.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, fields
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Final, Literal

TASK_NAME: Final[str] = "DarkFac-Usage-Cloud-Sync"
TASK_XML_NAMESPACE: Final[str] = "http://schemas.microsoft.com/windows/2004/02/mit/task"
SYNC_SCRIPT_PARTS: Final[tuple[str, str]] = ("scripts", "sync_usage_to_cloud.py")
DEFAULT_TARGET_URL: Final[str] = "https://darkhub.ggcampos.com"
DEFAULT_INTERVAL_S: Final[int] = 60
MAX_INTERVAL_S: Final[int] = 86_400

LOGON_TRIGGER: Final[str] = "LogonTrigger"
LOGON_TYPE: Final[str] = "InteractiveToken"
RUN_LEVEL: Final[str] = "LeastPrivilege"

# Directory layouts that hold temporary/linked worktrees. A scheduled task that
# points inside one of them dies (or silently runs stale code) when the worktree
# is cleaned up. ``.worktrees`` matches as a single segment; the others as a pair.
_WORKTREE_SEGMENT: Final[str] = ".worktrees"
_WORKTREE_PAIRS: Final[tuple[tuple[str, str], ...]] = (
    (".claude", "worktrees"),
    (".codex", "worktrees"),
)

Action = Literal["create", "keep", "update"]
RepoRoot = Path | str


class TaskXmlError(ValueError):
    """The task XML is malformed or does not describe a single Exec action."""


# ----------------------------------------------------------------------
# Definition
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class TaskSettings:
    """The ``<Settings>`` values that decide whether the loop keeps running.

    Defaults are the desired values. Idle settings are irrelevant for a logon
    task and are only emitted by :func:`build_task_xml`, never compared.
    """

    enabled: bool = True
    hidden: bool = True
    execution_time_limit: str = "PT0S"
    multiple_instances: str = "IgnoreNew"
    start_when_available: bool = True
    disallow_start_on_batteries: bool = False
    stop_if_going_on_batteries: bool = False
    restart_count: int = 3
    restart_interval: str = "PT1M"


# Task Scheduler schema defaults, applied when an element is missing from the XML.
_SCHEMA_DEFAULT_SETTINGS: Final[TaskSettings] = TaskSettings(
    enabled=True,
    hidden=False,
    execution_time_limit="PT72H",
    multiple_instances="IgnoreNew",
    start_when_available=False,
    disallow_start_on_batteries=True,
    stop_if_going_on_batteries=True,
    restart_count=0,
    restart_interval="",
)

_SETTING_LABELS: Final[dict[str, str]] = {
    "enabled": "Enabled",
    "hidden": "Hidden",
    "execution_time_limit": "ExecutionTimeLimit",
    "multiple_instances": "MultipleInstancesPolicy",
    "start_when_available": "StartWhenAvailable",
    "disallow_start_on_batteries": "DisallowStartIfOnBatteries",
    "stop_if_going_on_batteries": "StopIfGoingOnBatteries",
    "restart_count": "RestartOnFailure/Count",
    "restart_interval": "RestartOnFailure/Interval",
}


@dataclass(frozen=True)
class SyncTaskDefinition:
    """What the scheduled task runs and how it behaves (the compared surface)."""

    command: str
    arguments: str
    working_directory: str
    logon_type: str = LOGON_TYPE
    run_level: str = RUN_LEVEL
    triggers: tuple[str, ...] = (LOGON_TRIGGER,)
    settings: TaskSettings = field(default_factory=TaskSettings)

    @property
    def argument_tokens(self) -> list[str]:
        return split_arguments(self.arguments)

    @property
    def script(self) -> str:
        """The script path: the first argument of the Python command line."""
        tokens = self.argument_tokens
        return tokens[0] if tokens else ""


@dataclass(frozen=True)
class SyncTaskPlan:
    """The decision: ``create`` (absent), ``keep`` (already correct) or ``update``."""

    action: Action
    reasons: list[str]
    desired: SyncTaskDefinition
    existing: SyncTaskDefinition | None = None


@dataclass(frozen=True)
class QueryOutcome:
    """Interpretation of one ``schtasks /Query /TN <name> /XML`` result."""

    existing: SyncTaskDefinition | None
    unreadable: str | None = None  # detail when the task exists but cannot be parsed
    note: str = ""  # why ``existing`` is None (first line of the schtasks message)


# ----------------------------------------------------------------------
# Path helpers
# ----------------------------------------------------------------------


def _segments(path_text: str) -> list[str]:
    """Lower-cased path segments, splitting on both separators, ignoring ``.``/``..``/empties."""
    segments: list[str] = []
    for raw in re.split(r"[\\/]+", path_text):
        # Windows ignores trailing dots/spaces in a segment (".worktrees." is ".worktrees").
        segment = raw.strip().rstrip(" .").lower()
        if segment:
            segments.append(segment)
    return segments


def worktree_marker(path_text: str) -> str | None:
    """The worktree layout the path lies inside (``.worktrees``, ``.claude\\worktrees`` ...), else None.

    Case-insensitive and independent of the separator, so it works for Windows
    paths evaluated on Linux (CI) as well.
    """
    segments = _segments(str(path_text))
    if _WORKTREE_SEGMENT in segments:
        return _WORKTREE_SEGMENT
    for first, second in _WORKTREE_PAIRS:
        for index in range(len(segments) - 1):
            if segments[index] == first and segments[index + 1] == second:
                return f"{first}\\{second}"
    return None


def is_worktree_path(path_text: str) -> bool:
    return worktree_marker(path_text) is not None


def _norm_path(value: str) -> str:
    """Comparable form of a Windows path: unquoted, ``\\`` separators, no trailing slash, folded case."""
    text = value.strip().strip('"').strip().replace("/", "\\")
    prefix = "\\\\" if text.startswith("\\\\") else ""
    body = re.sub(r"\\+", r"\\", text[len(prefix):]).rstrip("\\")
    return (prefix + body).lower()


def split_arguments(text: str) -> list[str]:
    """Split a Windows command-line argument string, honouring double quotes."""
    tokens: list[str] = []
    buffer: list[str] = []
    in_quotes = False
    started = False
    for char in text:
        if char == '"':
            in_quotes = not in_quotes
            started = True
            continue
        if char.isspace() and not in_quotes:
            if started:
                tokens.append("".join(buffer))
                buffer = []
                started = False
            continue
        buffer.append(char)
        started = True
    if started:
        tokens.append("".join(buffer))
    return tokens


def _absolute_flavour(path_text: str) -> PureWindowsPath | PurePosixPath | None:
    """A pure path object when ``path_text`` is absolute under Windows or POSIX rules."""
    windows = PureWindowsPath(path_text)
    if windows.is_absolute():
        return windows
    posix = PurePosixPath(path_text)
    if posix.is_absolute():
        return posix
    return None


# ----------------------------------------------------------------------
# Desired definition
# ----------------------------------------------------------------------


def desired_definition(
    repo_root: RepoRoot,
    *,
    python_exe: str,
    target_url: str = DEFAULT_TARGET_URL,
    interval: int = DEFAULT_INTERVAL_S,
) -> SyncTaskDefinition:
    """The definition the host must have, anchored at the main checkout ``repo_root``.

    Raises :class:`ValueError` (fail closed) when the root or the interpreter is
    relative or lies inside a worktree, or when the URL/interval is unusable.
    """
    root_text = str(repo_root).strip()
    if not root_text:
        raise ValueError("raiz do repositorio vazia")
    marker = worktree_marker(root_text)
    if marker:
        raise ValueError(
            f"a raiz do repositorio esta dentro de uma worktree ({marker}): {root_text}. "
            "Use o checkout principal (por exemplo C:\\dev\\DarkFac)."
        )
    root = _absolute_flavour(root_text)
    if root is None:
        raise ValueError(f"a raiz do repositorio deve ser um caminho absoluto: {root_text}")
    if '"' in root_text:
        raise ValueError("a raiz do repositorio nao pode conter aspas")

    python_text = str(python_exe).strip()
    if not python_text or '"' in python_text:
        raise ValueError("executavel do Python vazio ou com aspas")
    if _absolute_flavour(python_text) is None:
        raise ValueError(f"o executavel do Python deve ser um caminho absoluto: {python_text}")
    python_marker = worktree_marker(python_text)
    if python_marker:
        raise ValueError(
            f"o executavel do Python esta dentro de uma worktree ({python_marker}): {python_text}"
        )

    url = str(target_url).strip()
    if not re.fullmatch(r"https?://[^\s\"]+", url):
        raise ValueError(f"URL de destino invalida: {target_url!r}")
    if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= MAX_INTERVAL_S:
        raise ValueError(f"intervalo invalido (1..{MAX_INTERVAL_S} segundos): {interval!r}")

    script = str(root.joinpath(*SYNC_SCRIPT_PARTS))
    return SyncTaskDefinition(
        command=python_text,
        arguments=f'"{script}" --target-url "{url}" --loop --interval {interval}',
        working_directory=str(root),
    )


# ----------------------------------------------------------------------
# Reading the existing task
# ----------------------------------------------------------------------

_XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>", re.IGNORECASE)


def _local(tag: Any) -> str:
    text = tag if isinstance(tag, str) else ""
    return text.rsplit("}", 1)[-1]


def _child(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None
    for candidate in element:
        if _local(candidate.tag) == name:
            return candidate
    return None


def _children(element: ET.Element | None) -> list[ET.Element]:
    return list(element) if element is not None else []


def _text(element: ET.Element | None, name: str, default: str) -> str:
    found = _child(element, name)
    if found is None or found.text is None:
        return default
    value = found.text.strip()
    return value if value else default


def _as_bool(value: str, default: bool) -> bool:
    lowered = value.strip().lower()
    if lowered in ("true", "1"):
        return True
    if lowered in ("false", "0"):
        return False
    return default


def parse_task_xml(xml_text: str) -> SyncTaskDefinition | None:
    """Parse the output of ``schtasks /Query /TN <name> /XML``.

    Returns ``None`` for blank input. Tolerates a BOM, an ``encoding="UTF-16"``
    declaration on text that is already decoded, and presence/absence of the
    Task Scheduler namespace. Raises :class:`TaskXmlError` for malformed XML or a
    task that does not have exactly one ``Exec`` action.
    """
    text = xml_text.lstrip("\ufeff").strip()
    if not text:
        return None
    text = _XML_DECLARATION.sub("", text, count=1)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise TaskXmlError(f"XML invalido: {exc}") from exc
    if _local(root.tag) != "Task":
        raise TaskXmlError("o elemento raiz nao e <Task>")

    execs = [node for node in _children(_child(root, "Actions")) if _local(node.tag) == "Exec"]
    if len(execs) != 1:
        raise TaskXmlError(f"esperada exatamente uma acao Exec, encontradas {len(execs)}")
    exec_node = execs[0]
    command = _text(exec_node, "Command", "")
    if not command:
        raise TaskXmlError("a acao Exec nao tem <Command>")

    principal = _child(_child(root, "Principals"), "Principal")
    logon_type = _text(principal, "LogonType", "")
    run_level = _text(principal, "RunLevel", RUN_LEVEL)

    trigger_kinds: list[str] = []
    for trigger in _children(_child(root, "Triggers")):
        kind = _local(trigger.tag)
        if not _as_bool(_text(trigger, "Enabled", "true"), True):
            kind += "[disabled]"
        trigger_kinds.append(kind)

    settings_node = _child(root, "Settings")
    base = _SCHEMA_DEFAULT_SETTINGS
    restart = _child(settings_node, "RestartOnFailure")
    try:
        restart_count = int(_text(restart, "Count", str(base.restart_count)))
    except ValueError as exc:
        raise TaskXmlError(f"RestartOnFailure/Count invalido: {exc}") from exc
    settings = TaskSettings(
        enabled=_as_bool(_text(settings_node, "Enabled", "true"), base.enabled),
        hidden=_as_bool(_text(settings_node, "Hidden", "false"), base.hidden),
        execution_time_limit=_text(settings_node, "ExecutionTimeLimit", base.execution_time_limit),
        multiple_instances=_text(settings_node, "MultipleInstancesPolicy", base.multiple_instances),
        start_when_available=_as_bool(
            _text(settings_node, "StartWhenAvailable", "false"), base.start_when_available
        ),
        disallow_start_on_batteries=_as_bool(
            _text(settings_node, "DisallowStartIfOnBatteries", "true"), base.disallow_start_on_batteries
        ),
        stop_if_going_on_batteries=_as_bool(
            _text(settings_node, "StopIfGoingOnBatteries", "true"), base.stop_if_going_on_batteries
        ),
        restart_count=restart_count,
        restart_interval=_text(restart, "Interval", base.restart_interval),
    )
    return SyncTaskDefinition(
        command=command,
        arguments=_text(exec_node, "Arguments", ""),
        working_directory=_text(exec_node, "WorkingDirectory", ""),
        logon_type=logon_type,
        run_level=run_level,
        triggers=tuple(sorted(trigger_kinds)),
        settings=settings,
    )


def decode_schtasks_output(data: bytes, *, fallback_encoding: str = "cp1252") -> str:
    """Decode raw ``schtasks`` output: UTF-16 (BOM or NUL-interleaved), UTF-8, else the console code page."""
    if not data:
        return ""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace").lstrip("\ufeff")
    if data.count(b"\x00") * 4 >= len(data):
        codec = "utf-16-be" if data[0] == 0 else "utf-16-le"
        return data.decode(codec, errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode(fallback_encoding, errors="replace")
    except LookupError:
        return data.decode("latin-1")


def _first_line(*texts: str) -> str:
    for text in texts:
        for line in text.splitlines():
            if line.strip():
                return line.strip()
    return ""


def interpret_query(returncode: int, stdout: str, stderr: str = "") -> QueryOutcome:
    """Turn one ``schtasks /Query /TN <name> /XML`` result into a :class:`QueryOutcome`.

    A non-zero exit is treated as "task absent" (schtasks uses exit 1 for every
    failure and its text is localised, so it cannot be told apart reliably);
    registering the desired definition afterwards is the safe outcome either way.
    An existing task whose XML cannot be read is flagged ``unreadable`` so it is
    never kept.
    """
    if returncode != 0:
        return QueryOutcome(existing=None, note=_first_line(stderr, stdout))
    try:
        parsed = parse_task_xml(stdout)
    except TaskXmlError as exc:
        return QueryOutcome(existing=None, unreadable=str(exc))
    if parsed is None:
        return QueryOutcome(existing=None, unreadable="saida vazia")
    return QueryOutcome(existing=parsed)


# ----------------------------------------------------------------------
# Planning
# ----------------------------------------------------------------------


def _fmt(value: object) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def _differences(existing: SyncTaskDefinition, desired: SyncTaskDefinition) -> list[str]:
    reasons: list[str] = []
    flagged: set[str] = set()

    # Worktree pointers first: they are the incident this module exists to prevent.
    seen_paths: set[str] = set()
    for label, value in (
        ("command", existing.command),
        ("script", existing.script),
        ("working_directory", existing.working_directory),
    ):
        if value and is_worktree_path(value):
            flagged.add(label)
            if value not in seen_paths:
                seen_paths.add(value)
                reasons.append(f"aponta para worktree: {value}")

    if "command" not in flagged and _norm_path(existing.command) != _norm_path(desired.command):
        reasons.append(f"executavel diferente: {existing.command} (esperado {desired.command})")
    if "script" not in flagged and _norm_path(existing.script) != _norm_path(desired.script):
        reasons.append(f"script diferente: {existing.script or '(vazio)'} (esperado {desired.script})")
    existing_rest = existing.argument_tokens[1:]
    desired_rest = desired.argument_tokens[1:]
    if existing_rest != desired_rest:
        reasons.append(
            f"argumentos diferentes: {' '.join(existing_rest) or '(vazio)'} (esperado {' '.join(desired_rest)})"
        )
    if "working_directory" not in flagged and _norm_path(existing.working_directory) != _norm_path(
        desired.working_directory
    ):
        reasons.append(
            "diretorio de trabalho diferente: "
            f"{existing.working_directory or '(vazio)'} (esperado {desired.working_directory})"
        )

    if existing.logon_type != desired.logon_type:
        reasons.append(
            f"tipo de logon diferente: {existing.logon_type or '(vazio)'} (esperado {desired.logon_type})"
        )
    if existing.run_level != desired.run_level:
        reasons.append(f"nivel de execucao diferente: {existing.run_level} (esperado {desired.run_level})")
    if existing.triggers != desired.triggers:
        reasons.append(
            f"gatilhos diferentes: {', '.join(existing.triggers) or '(nenhum)'} "
            f"(esperado {', '.join(desired.triggers)})"
        )
    for item in fields(TaskSettings):
        have = getattr(existing.settings, item.name)
        want = getattr(desired.settings, item.name)
        if have != want:
            reasons.append(
                f"configuracao diferente: {_SETTING_LABELS[item.name]}={_fmt(have)} (esperado {_fmt(want)})"
            )
    return reasons


def plan(
    existing: SyncTaskDefinition | None,
    desired: SyncTaskDefinition,
    *,
    unreadable: str | None = None,
) -> SyncTaskPlan:
    """Decide what to do with the task.

    * ``unreadable`` set (task exists but could not be parsed) -> ``update``, never ``keep``.
    * ``existing`` is None -> ``create``.
    * Otherwise ``keep`` when nothing differs, else ``update`` with every reason.
    """
    if unreadable is not None:
        reason = f"definicao ilegivel: {unreadable}" if unreadable else "definicao ilegivel"
        return SyncTaskPlan(action="update", reasons=[reason], desired=desired, existing=None)
    if existing is None:
        return SyncTaskPlan(action="create", reasons=["tarefa ausente"], desired=desired, existing=None)
    reasons = _differences(existing, desired)
    return SyncTaskPlan(
        action="update" if reasons else "keep",
        reasons=reasons,
        desired=desired,
        existing=existing,
    )


# ----------------------------------------------------------------------
# Registration XML
# ----------------------------------------------------------------------

_TASK_DESCRIPTION = (
    "DarkFac quota meter: syncs local AI account usage to DarkHub every cycle. "
    "Managed by scripts/install_usage_sync_task.py; always points to the main checkout."
)


def _add(parent: ET.Element, tag: str, text: str | None = None, **attrib: str) -> ET.Element:
    element = ET.SubElement(parent, tag, attrib)
    if text is not None:
        element.text = text
    return element


def build_task_xml(definition: SyncTaskDefinition, user: str) -> str:
    """The Task Scheduler XML to register (``schtasks /Create /XML``), declared as UTF-16.

    The caller must write it to disk as UTF-16. Only the per-user logon design
    (InteractiveToken, one LogonTrigger) is supported; anything else raises.
    """
    account = user.strip()
    if not account:
        raise ValueError("usuario vazio")
    if definition.triggers != (LOGON_TRIGGER,) or definition.logon_type != LOGON_TYPE:
        raise ValueError("so a tarefa por usuario (LogonTrigger + InteractiveToken) e suportada")
    settings = definition.settings

    root = ET.Element("Task", {"version": "1.3", "xmlns": TASK_XML_NAMESPACE})
    registration = _add(root, "RegistrationInfo")
    _add(registration, "Description", _TASK_DESCRIPTION)

    triggers = _add(root, "Triggers")
    logon = _add(triggers, LOGON_TRIGGER)
    _add(logon, "Enabled", "true")
    _add(logon, "UserId", account)

    principals = _add(root, "Principals")
    principal = _add(principals, "Principal", id="Author")
    _add(principal, "UserId", account)
    _add(principal, "LogonType", definition.logon_type)
    _add(principal, "RunLevel", definition.run_level)

    node = _add(root, "Settings")
    _add(node, "MultipleInstancesPolicy", settings.multiple_instances)
    _add(node, "DisallowStartIfOnBatteries", _fmt(settings.disallow_start_on_batteries))
    _add(node, "StopIfGoingOnBatteries", _fmt(settings.stop_if_going_on_batteries))
    _add(node, "StartWhenAvailable", _fmt(settings.start_when_available))
    idle = _add(node, "IdleSettings")
    _add(idle, "Duration", "PT10M")
    _add(idle, "WaitTimeout", "PT1H")
    _add(idle, "StopOnIdleEnd", "true")
    _add(idle, "RestartOnIdle", "false")
    _add(node, "Enabled", _fmt(settings.enabled))
    _add(node, "Hidden", _fmt(settings.hidden))
    _add(node, "ExecutionTimeLimit", settings.execution_time_limit)
    restart = _add(node, "RestartOnFailure")
    _add(restart, "Interval", settings.restart_interval)
    _add(restart, "Count", str(settings.restart_count))
    _add(node, "UseUnifiedSchedulingEngine", "true")

    actions = _add(root, "Actions", Context="Author")
    exec_node = _add(actions, "Exec")
    _add(exec_node, "Command", definition.command)
    _add(exec_node, "Arguments", definition.arguments)
    _add(exec_node, "WorkingDirectory", definition.working_directory)

    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-16"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
