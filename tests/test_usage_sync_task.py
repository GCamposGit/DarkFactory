"""USR-236 (slice 5 of USR-206): the usage-sync scheduled task must never point at a worktree.

Covers the pure decision module ``core.usage.sync_task`` and the thin installer
``scripts/install_usage_sync_task.py``. Hermetic: no real ``schtasks``, no network;
every Windows path is spelled as a string so the suite behaves the same on Linux and Windows.

Reachability Contract: python -m pytest tests/test_usage_sync_task.py -q
"""

from __future__ import annotations

import json
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import scripts.install_usage_sync_task as install
from core.usage.sync_task import (
    TASK_NAME,
    TASK_XML_NAMESPACE,
    SyncTaskDefinition,
    TaskXmlError,
    build_task_xml,
    decode_schtasks_output,
    desired_definition,
    interpret_query,
    parse_task_xml,
    plan,
    split_arguments,
    worktree_marker,
)

PYTHONW = r"C:\Users\example\AppData\Local\Programs\Python\Python312\pythonw.exe"
MAIN_ROOT = r"C:\dev\DarkFac"
USER = r"EXAMPLE-PC\darkfac-user"
NS = {"t": TASK_XML_NAMESPACE}

# Sanitized copy of the task as `schtasks /Query /TN DarkFac-Usage-Cloud-Sync /XML`
# returned it on 2026-10-11 after the manual fix (fictitious SID/user/profile path).
LIVE_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <URI>\\DarkFac-Usage-Cloud-Sync</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-21-1111111111-2222222222-3333333333-1001</UserId>
      <LogonType>InteractiveToken</LogonType>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Hidden>true</Hidden>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <RestartOnFailure>
      <Count>3</Count>
      <Interval>PT1M</Interval>
    </RestartOnFailure>
    <StartWhenAvailable>true</StartWhenAvailable>
    <IdleSettings>
      <Duration>PT10M</Duration>
      <WaitTimeout>PT1H</WaitTimeout>
      <StopOnIdleEnd>true</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
  </Settings>
  <Triggers>
    <LogonTrigger>
      <UserId>EXAMPLE-PC\\darkfac-user</UserId>
    </LogonTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>C:\\Users\\example\\AppData\\Local\\Programs\\Python\\Python312\\pythonw.exe</Command>
      <Arguments>"C:\\dev\\DarkFac\\scripts\\sync_usage_to_cloud.py" --target-url "https://darkhub.ggcampos.com" --loop --interval 60</Arguments>
      <WorkingDirectory>C:\\dev\\DarkFac</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""


def _desired(root: str = MAIN_ROOT, **overrides: Any) -> SyncTaskDefinition:
    params: dict[str, Any] = {"python_exe": PYTHONW, "target_url": "https://darkhub.ggcampos.com", "interval": 60}
    params.update(overrides)
    return desired_definition(root, **params)


DESIRED = _desired()


def _mutated(old: str, new: str, xml: str = LIVE_XML) -> str:
    result = xml.replace(old, new)
    assert result != xml, f"fixture does not contain {old!r}"
    return result


def _plan_for(xml: str, desired: SyncTaskDefinition = DESIRED):
    existing = parse_task_xml(xml)
    assert existing is not None
    return plan(existing, desired)


# ----------------------------------------------------------------------
# parse_task_xml
# ----------------------------------------------------------------------


def test_parse_live_task_xml_reads_every_compared_field() -> None:
    task = parse_task_xml(LIVE_XML)

    assert task is not None
    assert task.command == PYTHONW
    assert task.script == r"C:\dev\DarkFac\scripts\sync_usage_to_cloud.py"
    assert task.argument_tokens[1:] == ["--target-url", "https://darkhub.ggcampos.com", "--loop", "--interval", "60"]
    assert task.working_directory == MAIN_ROOT
    assert task.logon_type == "InteractiveToken"
    assert task.triggers == ("LogonTrigger",)
    settings = task.settings
    assert settings.hidden is True
    assert settings.execution_time_limit == "PT0S"
    assert settings.multiple_instances == "IgnoreNew"
    assert settings.start_when_available is True
    assert settings.disallow_start_on_batteries is False
    assert settings.stop_if_going_on_batteries is False
    assert (settings.restart_count, settings.restart_interval) == (3, "PT1M")
    assert settings.enabled is True


def test_live_task_already_matches_desired_so_plan_is_keep() -> None:
    result = _plan_for(LIVE_XML)

    assert result.action == "keep"
    assert result.reasons == []


@pytest.mark.parametrize(
    "variant",
    [
        pytest.param(lambda xml: "\ufeff" + xml, id="bom"),
        pytest.param(lambda xml: xml.replace(f' xmlns="{TASK_XML_NAMESPACE}"', ""), id="no-namespace"),
        pytest.param(lambda xml: xml.replace('encoding="UTF-16"', 'encoding="UTF-8"'), id="utf8-declaration"),
    ],
)
def test_parse_tolerates_bom_declaration_and_namespace(variant: Any) -> None:
    assert parse_task_xml(variant(LIVE_XML)) == parse_task_xml(LIVE_XML)


def test_parse_blank_text_is_none() -> None:
    assert parse_task_xml("") is None
    assert parse_task_xml("  \r\n") is None
    assert parse_task_xml("\ufeff") is None


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("this is not xml", id="not-xml"),
        pytest.param("<Other/>", id="wrong-root"),
        pytest.param('<Task xmlns="urn:x"><Actions><ComHandler/></Actions></Task>', id="no-exec"),
        pytest.param(
            "<Task><Actions><Exec><Command>a.exe</Command></Exec><Exec><Command>b.exe</Command></Exec></Actions></Task>",
            id="two-exec",
        ),
    ],
)
def test_parse_rejects_unreadable_tasks(text: str) -> None:
    with pytest.raises(TaskXmlError):
        parse_task_xml(text)


def test_parse_applies_task_scheduler_schema_defaults_for_missing_settings() -> None:
    minimal = "<Task><Actions><Exec><Command>a.exe</Command></Exec></Actions></Task>"
    task = parse_task_xml(minimal)

    assert task is not None
    assert task.settings.execution_time_limit == "PT72H"  # would kill the loop after three days
    assert task.settings.hidden is False
    assert task.settings.disallow_start_on_batteries is True
    assert task.settings.restart_count == 0
    assert task.triggers == ()


# ----------------------------------------------------------------------
# plan
# ----------------------------------------------------------------------


def test_plan_creates_when_task_is_absent() -> None:
    result = plan(None, DESIRED)

    assert result.action == "create"
    assert result.reasons == ["tarefa ausente"]
    assert result.existing is None


def test_plan_updates_when_existing_definition_is_unreadable() -> None:
    result = plan(None, DESIRED, unreadable="XML invalido: x")

    assert result.action == "update"
    assert result.reasons == ["definicao ilegivel: XML invalido: x"]
    assert plan(None, DESIRED, unreadable="").reasons == ["definicao ilegivel"]


def test_plan_flags_the_codex_worktree_from_the_2026_10_10_incident() -> None:
    stale = r"C:\Users\example\.codex\worktrees\usr-124-postmerge"
    result = _plan_for(_mutated(MAIN_ROOT, stale))

    assert result.action == "update"
    assert result.reasons[0] == rf"aponta para worktree: {stale}\scripts\sync_usage_to_cloud.py"
    assert result.reasons[1] == f"aponta para worktree: {stale}"
    assert not any(reason.startswith("script diferente") for reason in result.reasons)


@pytest.mark.parametrize(
    "stale_root",
    [
        r"C:\dev\DarkFac\.worktrees\usr-236-20261011T170528Z",
        r"C:\dev\DarkFac\.claude\worktrees\agent-1",
        r"C:\Users\example\.codex\worktrees\usr-124-postmerge",
        r"c:\DEV\darkfac\.WorkTrees\Usr-9",
    ],
)
def test_plan_updates_any_task_that_points_into_a_worktree(stale_root: str) -> None:
    result = _plan_for(_mutated(MAIN_ROOT, stale_root))

    assert result.action == "update"
    assert any(reason.startswith("aponta para worktree:") for reason in result.reasons)


def test_plan_reports_a_different_checkout_without_calling_it_a_worktree() -> None:
    result = _plan_for(_mutated(MAIN_ROOT, r"D:\factory\DarkFac"))

    assert result.action == "update"
    assert any(reason.startswith("script diferente:") for reason in result.reasons)
    assert any(reason.startswith("diretorio de trabalho diferente:") for reason in result.reasons)
    assert not any("worktree" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param("--interval 60", "--interval 30", id="interval"),
        pytest.param('--target-url "https://darkhub.ggcampos.com"', '--target-url "https://example.org"', id="url"),
        pytest.param(" --loop", "", id="no-loop"),
    ],
)
def test_plan_updates_when_arguments_differ(old: str, new: str) -> None:
    result = _plan_for(_mutated(old, new))

    assert result.action == "update"
    assert [reason for reason in result.reasons if reason.startswith("argumentos diferentes:")]


def test_plan_ignores_case_separator_trailing_slash_and_quoting_differences() -> None:
    same = _mutated(
        r'"C:\dev\DarkFac\scripts\sync_usage_to_cloud.py"',
        r"c:/DEV/darkfac/Scripts/sync_usage_to_cloud.py",
    )
    same = _mutated(f"<WorkingDirectory>{MAIN_ROOT}</WorkingDirectory>", "<WorkingDirectory>C:\\dev\\darkfac\\</WorkingDirectory>", same)

    assert _plan_for(same).action == "keep"


def test_plan_updates_when_the_python_executable_differs() -> None:
    result = _plan_for(_mutated(PYTHONW, r"C:\Python311\pythonw.exe"))

    assert result.action == "update"
    assert [reason for reason in result.reasons if reason.startswith("executavel diferente:")]


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        pytest.param("<ExecutionTimeLimit>PT0S<", "<ExecutionTimeLimit>PT72H<", "ExecutionTimeLimit=PT72H", id="time-limit"),
        pytest.param("<Hidden>true<", "<Hidden>false<", "Hidden=false", id="hidden"),
        pytest.param("<MultipleInstancesPolicy>IgnoreNew<", "<MultipleInstancesPolicy>Parallel<", "MultipleInstancesPolicy=Parallel", id="instances"),
        pytest.param("<DisallowStartIfOnBatteries>false<", "<DisallowStartIfOnBatteries>true<", "DisallowStartIfOnBatteries=true", id="battery"),
        pytest.param("<Count>3<", "<Count>0<", "RestartOnFailure/Count=0", id="restart"),
        pytest.param("<Settings>", "<Settings><Enabled>false</Enabled>", "Enabled=false", id="task-disabled"),
        pytest.param("<LogonType>InteractiveToken<", "<LogonType>Password<", "tipo de logon diferente", id="logon-type"),
        pytest.param("LogonTrigger", "BootTrigger", "gatilhos diferentes", id="trigger-kind"),
        pytest.param("<LogonTrigger>", "<LogonTrigger><Enabled>false</Enabled>", "LogonTrigger[disabled]", id="trigger-disabled"),
    ],
)
def test_plan_updates_when_behavior_settings_drift(old: str, new: str, expected: str) -> None:
    result = _plan_for(_mutated(old, new))

    assert result.action == "update"
    assert any(expected in reason for reason in result.reasons), result.reasons


# ----------------------------------------------------------------------
# desired_definition
# ----------------------------------------------------------------------


def test_desired_definition_matches_the_live_task_and_accepts_path_or_str() -> None:
    expected = SyncTaskDefinition(
        command=PYTHONW,
        arguments=r'"C:\dev\DarkFac\scripts\sync_usage_to_cloud.py" --target-url "https://darkhub.ggcampos.com" --loop --interval 60',
        working_directory=MAIN_ROOT,
    )

    assert DESIRED == expected
    assert _desired("C:/dev/DarkFac/") == expected
    assert desired_definition(Path(MAIN_ROOT), python_exe=PYTHONW, target_url="https://darkhub.ggcampos.com", interval=60) == expected


@pytest.mark.parametrize(
    "root",
    [
        r"C:\dev\DarkFac\.worktrees\usr-236-20261011T170528Z",
        r"C:\dev\DarkFac\.claude\worktrees\agent-1",
        r"C:\Users\guigc\.codex\worktrees\usr-124-postmerge",
        "C:/dev/DarkFac/.worktrees/usr-236",
        "C:/Users/guigc/.CODEX/Worktrees/usr-124-postmerge",
        r"c:\dev\darkfac\.CLAUDE\WORKTREES\x",
        r"C:\dev\DarkFac\.worktrees",
        r"C:\dev\DarkFac/.worktrees\sub/dir",
    ],
)
def test_desired_definition_refuses_worktree_roots(root: str) -> None:
    with pytest.raises(ValueError, match="worktree"):
        _desired(root)


def test_desired_definition_refuses_a_python_inside_a_worktree() -> None:
    with pytest.raises(ValueError, match="worktree"):
        _desired(python_exe=r"C:\dev\DarkFac\.worktrees\x\.venv\Scripts\pythonw.exe")


@pytest.mark.parametrize(
    "root",
    [r"C:\dev\worktrees-archive\DarkFac", r"C:\dev\my.worktrees\DarkFac", r"C:\dev\.claude\DarkFac", r"D:\.codex\DarkFac"],
)
def test_desired_definition_accepts_lookalike_directory_names(root: str) -> None:
    assert _desired(root).working_directory == root


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"repo_root": ""}, id="empty-root"),
        pytest.param({"repo_root": r"dev\DarkFac"}, id="relative-root"),
        pytest.param({"repo_root": 'C:\\dev\\"DarkFac'}, id="quote-in-root"),
        pytest.param({"python_exe": "pythonw.exe"}, id="relative-python"),
        pytest.param({"python_exe": ""}, id="empty-python"),
        pytest.param({"target_url": "ftp://darkhub"}, id="bad-url-scheme"),
        pytest.param({"target_url": 'https://x" --evil'}, id="url-injection"),
        pytest.param({"interval": 0}, id="zero-interval"),
        pytest.param({"interval": 10**9}, id="huge-interval"),
        pytest.param({"interval": True}, id="bool-interval"),
    ],
)
def test_desired_definition_rejects_unusable_inputs(overrides: dict[str, Any]) -> None:
    root = overrides.pop("repo_root", MAIN_ROOT)
    with pytest.raises(ValueError):
        _desired(root, **overrides)


def test_worktree_marker_reports_the_layout() -> None:
    assert worktree_marker(r"C:\a\.worktrees\b") == ".worktrees"
    assert worktree_marker("c:/a/.Claude/Worktrees/b") == ".claude\\worktrees"
    assert worktree_marker(r"C:\a\.codex\worktrees") == ".codex\\worktrees"
    assert worktree_marker(r"C:\dev\DarkFac") is None
    assert worktree_marker(r"C:\dev\worktrees\x") is None


def test_split_arguments_honours_quotes() -> None:
    assert split_arguments('"C:\\a b\\x.py" --flag "v w" --n 3 ""') == ["C:\\a b\\x.py", "--flag", "v w", "--n", "3", ""]
    assert split_arguments("") == []


# ----------------------------------------------------------------------
# build_task_xml
# ----------------------------------------------------------------------


def _build_root(definition: SyncTaskDefinition = DESIRED) -> tuple[str, ET.Element]:
    xml = build_task_xml(definition, USER)
    declaration, _, body = xml.partition("?>")
    assert declaration == '<?xml version="1.0" encoding="UTF-16"'
    return xml, ET.fromstring(body)


def _value(root: ET.Element, path: str) -> str:
    node = root.find(path, NS)
    assert node is not None, path
    return node.text or ""


def test_build_task_xml_is_well_formed_and_carries_every_required_setting() -> None:
    xml, root = _build_root()

    assert root.tag == f"{{{TASK_XML_NAMESPACE}}}Task"
    assert _value(root, "t:Principals/t:Principal/t:UserId") == USER
    assert _value(root, "t:Principals/t:Principal/t:LogonType") == "InteractiveToken"
    assert _value(root, "t:Triggers/t:LogonTrigger/t:UserId") == USER
    assert _value(root, "t:Actions/t:Exec/t:Command") == PYTHONW
    assert _value(root, "t:Actions/t:Exec/t:Arguments") == DESIRED.arguments
    assert _value(root, "t:Actions/t:Exec/t:WorkingDirectory") == MAIN_ROOT
    expected_settings = {
        "t:Settings/t:Hidden": "true",
        "t:Settings/t:ExecutionTimeLimit": "PT0S",
        "t:Settings/t:MultipleInstancesPolicy": "IgnoreNew",
        "t:Settings/t:StartWhenAvailable": "true",
        "t:Settings/t:DisallowStartIfOnBatteries": "false",
        "t:Settings/t:StopIfGoingOnBatteries": "false",
        "t:Settings/t:RestartOnFailure/t:Count": "3",
        "t:Settings/t:RestartOnFailure/t:Interval": "PT1M",
        "t:Settings/t:IdleSettings/t:Duration": "PT10M",
        "t:Settings/t:IdleSettings/t:WaitTimeout": "PT1H",
        "t:Settings/t:IdleSettings/t:StopOnIdleEnd": "true",
        "t:Settings/t:IdleSettings/t:RestartOnIdle": "false",
    }
    for path, expected in expected_settings.items():
        assert _value(root, path) == expected, path
    assert "TELEMETRY" not in xml.upper()  # credentials never go into task metadata


def test_build_task_xml_round_trips_to_keep_so_a_second_run_changes_nothing() -> None:
    registered = parse_task_xml(build_task_xml(DESIRED, USER))

    assert registered == DESIRED
    assert plan(registered, DESIRED).action == "keep"


def test_build_task_xml_escapes_special_characters_in_paths() -> None:
    desired = _desired(r"C:\dev\Dark&Fac")
    _, root = _build_root(desired)

    assert _value(root, "t:Actions/t:Exec/t:WorkingDirectory") == r"C:\dev\Dark&Fac"
    assert parse_task_xml(build_task_xml(desired, USER)) == desired


def test_build_task_xml_rejects_unsupported_designs() -> None:
    with pytest.raises(ValueError):
        build_task_xml(replace(DESIRED, triggers=("TimeTrigger",)), USER)
    with pytest.raises(ValueError):
        build_task_xml(replace(DESIRED, logon_type="Password"), USER)
    with pytest.raises(ValueError):
        build_task_xml(DESIRED, "  ")


# ----------------------------------------------------------------------
# interpret_query / decode_schtasks_output
# ----------------------------------------------------------------------


def test_interpret_query_covers_absent_present_and_unreadable_tasks() -> None:
    absent = interpret_query(1, "", "ERROR: The system cannot find the file specified.\n")
    assert (absent.existing, absent.unreadable) == (None, None)
    assert absent.note == "ERROR: The system cannot find the file specified."

    present = interpret_query(0, LIVE_XML, "")
    assert present.existing is not None and present.unreadable is None

    garbage = interpret_query(0, "<Task><oops", "")
    assert garbage.existing is None and garbage.unreadable and "XML invalido" in garbage.unreadable

    empty = interpret_query(0, "  ", "")
    assert empty.existing is None and empty.unreadable == "saida vazia"
    assert plan(empty.existing, DESIRED, unreadable=empty.unreadable).action == "update"


@pytest.mark.parametrize(
    ("data", "kwargs"),
    [
        pytest.param(LIVE_XML.encode("utf-16"), {}, id="utf16-bom"),
        pytest.param(LIVE_XML.encode("utf-16-le"), {}, id="utf16-le-no-bom"),
        pytest.param(LIVE_XML.encode("utf-8"), {}, id="utf8"),
        pytest.param(LIVE_XML.encode("utf-8-sig"), {}, id="utf8-bom"),
        pytest.param(LIVE_XML.replace("example", "usu\u00e1rio").encode("cp850"), {"fallback_encoding": "cp850"}, id="oem-fallback"),
    ],
)
def test_decode_schtasks_output_handles_every_console_encoding(data: bytes, kwargs: dict[str, Any]) -> None:
    text = decode_schtasks_output(data, **kwargs)

    assert parse_task_xml(text) is not None
    assert decode_schtasks_output(b"") == ""


# ----------------------------------------------------------------------
# CLI (scripts/install_usage_sync_task.py) with an injected schtasks runner
# ----------------------------------------------------------------------


class FakeSchtasks:
    """Stateful stand-in for ``schtasks``: stores the XML that /Create receives."""

    def __init__(self, xml: str | None = None, *, create_rc: int = 0, store_on_create: bool = True) -> None:
        self.xml = xml
        self.create_rc = create_rc
        self.store_on_create = store_on_create
        self.calls: list[list[str]] = []
        self.created: list[str] = []

    def __call__(self, args: Any) -> subprocess.CompletedProcess[str]:
        argv = list(args)
        self.calls.append(argv)
        if argv[0] == "/Query":
            if self.xml is None:
                return subprocess.CompletedProcess(argv, 1, "", "ERROR: The system cannot find the file specified.")
            return subprocess.CompletedProcess(argv, 0, self.xml, "")
        if argv[0] == "/Create":
            raw = Path(argv[argv.index("/XML") + 1]).read_bytes()
            assert raw[:2] in (b"\xff\xfe", b"\xfe\xff"), "the XML file must be UTF-16 with BOM"
            text = raw.decode("utf-16")
            self.created.append(text)
            if self.create_rc:
                return subprocess.CompletedProcess(argv, self.create_rc, "", "ERROR: Access is denied.")
            if self.store_on_create:
                self.xml = text
            return subprocess.CompletedProcess(argv, 0, "SUCCESS", "")
        raise AssertionError(f"unexpected schtasks call: {argv}")

    @property
    def creates(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] == "/Create"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "DarkFac"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "sync_usage_to_cloud.py").write_text("# stub\n", encoding="utf-8")
    return root


def _argv(repo: Path, *extra: str) -> list[str]:
    return ["--repo-root", str(repo), "--python", PYTHONW, *extra]


def test_cli_plan_only_prints_the_plan_and_never_registers(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeSchtasks()

    assert install.main(_argv(repo), runner=fake, user=USER) == 0

    out = capsys.readouterr().out
    assert "Acao: create" in out and "tarefa ausente" in out and "--apply" in out
    assert fake.creates == []
    assert all(call[0] == "/Query" for call in fake.calls)


def test_cli_apply_registers_verifies_and_is_idempotent(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeSchtasks()

    assert install.main(_argv(repo, "--apply"), runner=fake, user=USER) == 0

    out = capsys.readouterr().out
    assert "Acao: create" in out and "registrada e verificada" in out
    assert len(fake.creates) == 1
    create = fake.creates[0]
    assert create[:4] == ["/Create", "/TN", TASK_NAME, "/XML"] and create[-1] == "/F"
    assert fake.xml is not None and f'UserId>{USER}<' in fake.xml

    assert install.main(_argv(repo, "--apply"), runner=fake, user=USER) == 0
    out = capsys.readouterr().out
    assert "Acao: keep" in out and "nada a fazer" in out
    assert len(fake.creates) == 1, "the second run must not touch the task"


def test_cli_apply_replaces_a_task_that_points_to_a_worktree(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    stale = _mutated(MAIN_ROOT, r"C:\Users\example\.codex\worktrees\usr-124-postmerge")
    fake = FakeSchtasks(stale)

    assert install.main(_argv(repo, "--apply"), runner=fake, user=USER) == 0

    out = capsys.readouterr().out
    assert "Acao: update" in out and "aponta para worktree:" in out and "schtasks /End" in out
    assert len(fake.creates) == 1
    assert plan(parse_task_xml(fake.xml or ""), desired_definition(repo.resolve(), python_exe=PYTHONW)).action == "keep"


def test_cli_treats_an_unreadable_existing_task_as_update(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeSchtasks("<Task><oops")

    assert install.main(_argv(repo, "--apply"), runner=fake, user=USER) == 0

    assert "definicao ilegivel" in capsys.readouterr().out
    assert len(fake.creates) == 1


def test_cli_exit_1_when_schtasks_create_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeSchtasks(create_rc=1)

    assert install.main(_argv(repo, "--apply"), runner=fake, user=USER) == 1

    assert "Administrador" in capsys.readouterr().err


def test_cli_exit_1_when_the_read_back_does_not_match(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeSchtasks(store_on_create=False)

    assert install.main(_argv(repo, "--apply", "--json"), runner=fake, user=USER) == 1

    report = json.loads(capsys.readouterr().out)
    assert report["applied"] is True and report["verified"] is False
    assert "verificacao posterior" in report["error"]


@pytest.mark.parametrize(
    "root",
    [r"C:\dev\DarkFac\.worktrees\usr-236", "C:/Users/x/.CODEX/Worktrees/usr-124-postmerge", r"C:\x\.claude\worktrees\a"],
)
def test_cli_refuses_worktree_repo_roots_with_exit_2(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeSchtasks()

    assert install.main(["--repo-root", root, "--python", PYTHONW, "--apply"], runner=fake, user=USER) == 2

    assert "worktree" in capsys.readouterr().err
    assert fake.calls == []


def test_cli_refuses_a_linked_worktree_even_outside_the_known_directories(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n", encoding="utf-8")
    fake = FakeSchtasks()

    assert install.main(_argv(repo, "--apply"), runner=fake, user=USER) == 2

    assert "worktree vinculada" in capsys.readouterr().err
    assert fake.calls == []


def test_cli_requires_the_sync_script_to_exist(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert install.main(_argv(tmp_path), runner=FakeSchtasks(), user=USER) == 2

    assert "nao encontrado" in capsys.readouterr().err


@pytest.mark.parametrize("interval", ["0", "abc"])
def test_cli_rejects_a_bad_interval_with_exit_2(repo: Path, interval: str) -> None:
    fake = FakeSchtasks()

    assert install.main(_argv(repo, "--interval", interval), runner=fake, user=USER) == 2

    assert fake.calls == []


def test_cli_json_report_has_the_documented_shape(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert install.main(_argv(repo, "--json"), runner=FakeSchtasks(), user=USER) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["task"] == TASK_NAME and report["mode"] == "plan"
    assert report["action"] == "create" and report["reasons"] == ["tarefa ausente"]
    assert report["desired"]["working_directory"] == str(repo.resolve())
    assert report["existing"] is None and report["applied"] is False and report["exit_code"] == 0


def test_cli_default_root_is_the_main_checkout_never_the_cwd(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[Path] = []

    def fake_main_checkout_root(cwd: Path) -> Path:
        seen.append(Path(cwd))
        return repo

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(install, "main_checkout_root", fake_main_checkout_root)

    assert install.main(["--python", PYTHONW, "--json"], runner=FakeSchtasks(), user=USER) == 0

    assert json.loads(capsys.readouterr().out)["desired"]["working_directory"] == str(repo.resolve())
    assert seen == [install.REPO_ROOT]


def test_cli_apply_without_an_injected_runner_requires_windows(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(_args: Any) -> None:
        raise AssertionError("schtasks must not run")

    monkeypatch.setattr(install, "_is_windows", lambda: False)
    monkeypatch.setattr(install, "run_schtasks", forbidden)

    assert install.main(_argv(repo, "--apply")) == 2

    assert "Windows" in capsys.readouterr().err


def test_resolve_python_defaults_to_pythonw_next_to_the_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "py"
    bindir.mkdir()
    (bindir / "python.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(install.sys, "executable", str(bindir / "python.exe"))

    with pytest.raises(install.InstallArgumentError, match="pythonw.exe"):
        install.resolve_python(None, check_exists=False)

    (bindir / "pythonw.exe").write_text("", encoding="utf-8")
    assert install.resolve_python(None, check_exists=False) == str(bindir / "pythonw.exe")
    assert install.resolve_python(PYTHONW, check_exists=False) == PYTHONW
    with pytest.raises(install.InstallArgumentError, match="--python"):
        install.resolve_python(str(bindir / "missing.exe"), check_exists=True)


def test_run_schtasks_uses_an_argument_list_without_a_shell_and_decodes_utf16(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, LIVE_XML.encode("utf-16"), b"")

    monkeypatch.setattr(install.subprocess, "run", fake_run)

    result = install.run_schtasks(["/Query", "/TN", TASK_NAME, "/XML"])

    assert isinstance(captured["cmd"], list) and captured["cmd"][1:] == ["/Query", "/TN", TASK_NAME, "/XML"]
    assert "schtasks" in captured["cmd"][0].lower()
    assert not captured["kwargs"].get("shell")
    assert parse_task_xml(result.stdout) == parse_task_xml(LIVE_XML)
    outcome = install.query_task(lambda args: result)
    assert plan(outcome.existing, DESIRED).action == "keep"


def test_text_report_is_plain_ascii_without_emoji() -> None:
    stale = parse_task_xml(_mutated(MAIN_ROOT, r"C:\Users\example\.codex\worktrees\usr-124-postmerge"))
    report = install.InstallReport(0, "plan", repo_root=MAIN_ROOT, plan=plan(stale, DESIRED))

    text = install.render_text(report)

    assert text.isascii()
    assert "aponta para worktree:" in text
