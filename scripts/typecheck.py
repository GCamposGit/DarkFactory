"""Verificacao de tipos real (mypy fixado) sobre modulos publicos limpos (USR-173).

Distinto de ``compileall`` (somente sintaxe). Falha fechado se o mypy faltar
ou divergir da versao fixada.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PINNED_MYPY = "1.13.0"
TYPED_MODULES: tuple[str, ...] = (
    "core/paths.py",
    "core/harness/models.py",
    "core/harness/markers.py",
    "core/harness/process_identity.py",
)


def run(files: tuple[str, ...] = TYPED_MODULES, root: Path = PROJECT_ROOT) -> int:
    try:
        version = subprocess.run(
            [sys.executable, "-m", "mypy", "--version"],
            capture_output=True, text=True, encoding="utf-8", check=False, cwd=root,
        )
    except OSError as exc:
        print(f"[TYPECHECK] FAIL: cannot run mypy: {exc}")
        return 2
    if version.returncode != 0 or PINNED_MYPY not in version.stdout:
        print(f"[TYPECHECK] FAIL: mypy=={PINNED_MYPY} required (pip install -r requirements.txt); got {version.stdout.strip() or version.stderr.strip()}")
        return 2
    print(f"[TYPECHECK] mypy {PINNED_MYPY} (static types, not syntax) on {len(files)} modules")
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "--config-file", str(root / "mypy.ini"), *files],
        text=True, encoding="utf-8", errors="replace", check=False, cwd=root,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    print(proc.stdout, end="")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(run())
