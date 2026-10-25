"""USR-384: boot service definitions and Windows task XML."""

from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from core.line.fleet_task_xml import TASK_NS, generate_task_xml


def test_windows_task_xml_runs_at_boot_as_service_user_and_restarts(tmp_path: Path) -> None:
    xml = generate_task_xml("MACHINE\\darkfac", Path("C:/Python312/python.exe"), tmp_path.resolve())
    root = ET.fromstring(xml)
    q = lambda name: f"{{{TASK_NS}}}{name}"

    assert root.find(f".//{q('BootTrigger')}") is not None
    assert root.findtext(f".//{q('UserId')}") == "MACHINE\\darkfac"
    assert root.findtext(f".//{q('LogonType')}") == "Password"
    assert root.findtext(f".//{q('RestartOnFailure')}/{q('Interval')}") == "PT1M"
    assert root.findtext(f".//{q('RestartOnFailure')}/{q('Count')}") == "999"
    assert root.findtext(f".//{q('Arguments')}") == "-m core.line.fleet_dispatcher --loop"
    assert root.findtext(f".//{q('WorkingDirectory')}") == str(tmp_path.resolve())


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
    assert "DARKFAC_FLEET_LOG_DIR=/var/log/darkfac" in unit
