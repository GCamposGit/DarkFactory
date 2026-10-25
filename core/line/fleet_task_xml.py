"""Task Scheduler XML generation for the Windows fleet dispatcher (USR-384)."""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path

TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
ET.register_namespace("", TASK_NS)


def generate_task_xml(user_id: str, python_path: Path, repo_root: Path) -> str:
    """Build a boot-triggered, restartable Task Scheduler 2.0 definition."""
    if not user_id.strip() or not python_path.is_absolute() or not repo_root.is_absolute():
        raise ValueError("user_id, python_path e repo_root absolutos sao obrigatorios")
    root = ET.Element(f"{{{TASK_NS}}}Task", {"version": "1.4"})
    reg = ET.SubElement(root, f"{{{TASK_NS}}}RegistrationInfo")
    ET.SubElement(reg, f"{{{TASK_NS}}}Description").text = "DarkFac fleet dispatcher (USR-384)"
    triggers = ET.SubElement(root, f"{{{TASK_NS}}}Triggers")
    ET.SubElement(triggers, f"{{{TASK_NS}}}BootTrigger")
    principals = ET.SubElement(root, f"{{{TASK_NS}}}Principals")
    principal = ET.SubElement(principals, f"{{{TASK_NS}}}Principal", {"id": "Author"})
    ET.SubElement(principal, f"{{{TASK_NS}}}UserId").text = user_id
    ET.SubElement(principal, f"{{{TASK_NS}}}LogonType").text = "Password"
    ET.SubElement(principal, f"{{{TASK_NS}}}RunLevel").text = "LeastPrivilege"
    settings = ET.SubElement(root, f"{{{TASK_NS}}}Settings")
    for name, value in (("MultipleInstancesPolicy", "IgnoreNew"), ("StartWhenAvailable", "true"),
                        ("DisallowStartIfOnBatteries", "false"), ("StopIfGoingOnBatteries", "false")):
        ET.SubElement(settings, f"{{{TASK_NS}}}{name}").text = value
    restart = ET.SubElement(settings, f"{{{TASK_NS}}}RestartOnFailure")
    ET.SubElement(restart, f"{{{TASK_NS}}}Interval").text = "PT1M"
    ET.SubElement(restart, f"{{{TASK_NS}}}Count").text = "999"
    ET.SubElement(settings, f"{{{TASK_NS}}}ExecutionTimeLimit").text = "PT0S"
    actions = ET.SubElement(root, f"{{{TASK_NS}}}Actions", {"Context": "Author"})
    exec_node = ET.SubElement(actions, f"{{{TASK_NS}}}Exec")
    ET.SubElement(exec_node, f"{{{TASK_NS}}}Command").text = str(python_path)
    ET.SubElement(exec_node, f"{{{TASK_NS}}}Arguments").text = "-m core.line.fleet_dispatcher --loop"
    ET.SubElement(exec_node, f"{{{TASK_NS}}}WorkingDirectory").text = str(repo_root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    args = parser.parse_args()
    print(generate_task_xml(args.user, args.python, args.repo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
