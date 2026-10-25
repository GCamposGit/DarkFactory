"""USR-384: boot service definitions and Windows task XML."""

import io
import logging
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from core.line.fleet_dispatcher import LOG_BACKUP_COUNT, LOG_MAX_BYTES, configure_logging
from core.line.fleet_task_xml import TASK_NS, generate_task_xml


def test_windows_task_xml_runs_at_boot_as_service_user_and_restarts(tmp_path: Path) -> None:
    xml = generate_task_xml("MACHINE\\darkfac", Path("C:/Python312/python.exe"), tmp_path.resolve())
    root = ET.fromstring(xml)
    q = lambda name: f"{{{TASK_NS}}}{name}"

    assert root.find(f".//{q('BootTrigger')}") is not None
    assert root.findtext(f".//{q('UserId')}") == "MACHINE\\darkfac"
    assert root.findtext(f".//{q('LogonType')}") == "Password"
    assert root.findtext(f".//{q('RunLevel')}") == "LeastPrivilege"
    assert root.findtext(f".//{q('MultipleInstancesPolicy')}") == "IgnoreNew"
    assert root.findtext(f".//{q('ExecutionTimeLimit')}") == "PT0S"
    assert root.findtext(f".//{q('RestartOnFailure')}/{q('Interval')}") == "PT1M"
    assert root.findtext(f".//{q('RestartOnFailure')}/{q('Count')}") == "999"
    assert root.findtext(f".//{q('Command')}") == "C:\\Python312\\python.exe"
    assert Path(root.findtext(f".//{q('Command')}")).is_absolute()
    assert root.findtext(f".//{q('Arguments')}") == "-m core.line.fleet_dispatcher --loop"
    assert root.findtext(f".//{q('WorkingDirectory')}") == str(tmp_path.resolve())
    assert Path(root.findtext(f".//{q('WorkingDirectory')}")).is_absolute()


def test_windows_task_xml_rejects_relative_executable_or_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        generate_task_xml("MACHINE\\darkfac", Path("python.exe"), tmp_path.resolve())
    with pytest.raises(ValueError):
        generate_task_xml("MACHINE\\darkfac", Path("C:/Python312/python.exe"), Path("relative"))


def test_systemd_unit_is_enabled_at_boot_and_restarts_on_failure() -> None:
    unit_path = Path(__file__).parents[1] / "scripts" / "fleet-dispatcher.service"
    unit = unit_path.read_text(encoding="utf-8")
    assert "Restart=always" in unit
    assert "RestartSec=10" in unit
    assert "WantedBy=multi-user.target" in unit
    assert "After=network-online.target" in unit
    assert "User=darkfac" in unit
    assert "Group=darkfac" in unit
    assert "EnvironmentFile=-/etc/darkfac/fleet-dispatcher.env" in unit
    assert "ExecStart=/opt/darkfac/.venv/bin/python -m core.line.fleet_dispatcher --loop" in unit
    assert "LogsDirectory=darkfac" in unit


def test_dispatcher_logging_rotates_utf8_file_and_keeps_console_copy(tmp_path: Path) -> None:
    console = io.StringIO()
    handler = configure_logging(tmp_path, stream=console)
    try:
        assert handler.maxBytes == LOG_MAX_BYTES == 10 * 1024 * 1024
        assert handler.backupCount == LOG_BACKUP_COUNT == 5
        assert handler.encoding == "utf-8"
        logging.getLogger("darkfac.test").info("despacho café")
        handler.flush()
        log_file = tmp_path / "fleet-dispatcher.log"
        assert "despacho café" in log_file.read_text(encoding="utf-8")
        assert "despacho café" in console.getvalue()
    finally:
        logging.shutdown()
