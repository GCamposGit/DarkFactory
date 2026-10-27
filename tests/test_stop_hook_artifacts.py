import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "stop_hook_artifacts.py"


def _git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path, with_code):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "README.md").write_text("x")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(tmp_path, "checkout", "-qb", "feat/usr-777-x")
    f = tmp_path / ("core/a.py" if with_code else "notes.md")
    f.parent.mkdir(exist_ok=True)
    f.write_text("y")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "feat: x [USR-777]")


def _run(cwd, **payload):
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps({"cwd": str(cwd), **payload}),
        capture_output=True,
        text=True,
    )


def test_blocks_code_without_pack(tmp_path):
    _repo(tmp_path, True)
    r = _run(tmp_path)
    assert r.returncode == 2 and "USR-777" in r.stderr


def test_allows_with_pack(tmp_path):
    _repo(tmp_path, True)
    p = tmp_path / ".factory" / "learning_packs"
    p.mkdir(parents=True)
    (p / "USR-777.md").write_text("pack")
    assert _run(tmp_path).returncode == 0


def test_allows_without_code_or_when_active(tmp_path):
    _repo(tmp_path, False)
    assert _run(tmp_path).returncode == 0
    other = tmp_path / "o"
    other.mkdir()
    _repo(other, True)
    assert _run(other, stop_hook_active=True).returncode == 0


def test_allows_on_unexpected_failure(tmp_path):
    r = _run(tmp_path)  # nao e repo git
    assert r.returncode == 0 and "ignorado" in r.stderr
