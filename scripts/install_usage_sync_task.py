"""Instalador idempotente da tarefa agendada DarkFac-Usage-Cloud-Sync (USR-236).

Uso:
    python C:\\dev\\DarkFac\\scripts\\install_usage_sync_task.py --check
    python C:\\dev\\DarkFac\\scripts\\install_usage_sync_task.py --apply

Exit: 0 = nada a fazer / aplicado; 1 = mudanca necessaria (--check) ou falha; 2 = recusado.
"""

from __future__ import annotations

import argparse
import getpass
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.usage.sync_task import (  # noqa: E402
    TASK_NAME,
    SyncTaskAction,
    SyncTaskState,
    decide_sync_task,
    expected_script_path,
)

SCHTASKS_TIMEOUT = 30
DEFAULT_TARGET_URL = "https://darkhub.ggcampos.com"
_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def main_checkout_root(start: Path = REPO) -> Path:
    """Raiz do checkout principal: pai do git-common-dir."""
    out = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        timeout=SCHTASKS_TIMEOUT,
        check=True,
    ).stdout.strip()
    return Path(out).parent


def _decode(raw: bytes) -> str:
    for enc in ("utf-16", "utf-8-sig", "mbcs"):
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if "<Task" in text:
            return text
    return raw.decode("utf-8", errors="replace")


def parse_task_xml(xml_text: str) -> SyncTaskState | None:
    """Extrai script e diretorio de trabalho da primeira acao Exec do XML da tarefa."""
    cleaned = xml_text.lstrip("\ufeff")
    if cleaned.startswith("<?xml"):
        cleaned = cleaned.split("?>", 1)[1]
    root = ET.fromstring(cleaned)
    exec_el = root.find(".//t:Actions/t:Exec", _NS)
    if exec_el is None:
        return None
    args = (exec_el.findtext("t:Arguments", default="", namespaces=_NS) or "").strip()
    if args.startswith('"') and args.count('"') >= 2:
        script = args.split('"')[1]
    else:
        script = args.split(" ")[0] if args else ""
    workdir = exec_el.findtext("t:WorkingDirectory", default="", namespaces=_NS) or ""
    return SyncTaskState(script_path=script, working_directory=workdir.strip())


def query_task() -> SyncTaskState | None:
    proc = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME, "/XML"],
        capture_output=True,
        timeout=SCHTASKS_TIMEOUT,
    )
    if proc.returncode != 0:
        return None
    return parse_task_xml(_decode(proc.stdout))


def _pythonw() -> str:
    found = shutil.which("pythonw.exe")
    if found:
        return found
    candidate = Path(sys.executable).with_name("pythonw.exe")
    if candidate.is_file():
        return str(candidate)
    raise FileNotFoundError("pythonw.exe nao encontrado; instale o runtime Python completo")


def build_task_xml(root: Path, target_url: str, user: str, pythonw: str) -> str:
    """XML equivalente ao de install_sync_task.ps1 (logon, oculta, loop de 60 s)."""
    script = expected_script_path(str(root))
    args = f'"{script}" --target-url "{target_url}" --loop --interval 60'
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Hidden>true</Hidden>
    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(pythonw)}</Command>
      <Arguments>{escape(args)}</Arguments>
      <WorkingDirectory>{escape(str(root))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def register_task(xml: str) -> None:
    tmp = REPO / ".factory" / "tmp_usage_sync_task.xml"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.write_bytes(xml.encode("utf-16"))
        subprocess.run(
            ["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/XML", str(tmp)],
            check=True,
            capture_output=True,
            timeout=SCHTASKS_TIMEOUT,
        )
    finally:
        tmp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="so informa o que faria")
    mode.add_argument("--apply", action="store_true", help="registra ou atualiza a tarefa")
    parser.add_argument("--target-url", default=DEFAULT_TARGET_URL)
    args = parser.parse_args(argv)

    root = main_checkout_root()
    decision = decide_sync_task(query_task(), str(root), str(REPO))
    print(f"[{decision.action.value.upper()}] {decision.reason}")
    if decision.action is SyncTaskAction.REFUSE:
        print(f"Execute: python {root}\\scripts\\install_usage_sync_task.py --apply", file=sys.stderr)
        return 2
    if decision.action is SyncTaskAction.KEEP:
        return 0
    if args.check:
        return 1
    register_task(build_task_xml(root, args.target_url, getpass.getuser(), _pythonw()))
    print(f"[OK] Tarefa '{TASK_NAME}' registrada apontando para {root}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
