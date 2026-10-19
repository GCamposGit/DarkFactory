"""USR-300: shard partition covers every test file exactly once; path classifier is conservative."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.paths import project_root
from core.testing import shards
from scripts import ci_paths


def test_partition_covers_all_files_exactly_once() -> None:
    files = shards.discover_test_files(project_root())
    assert len(files) > 50
    for total in (1, 2, 4, 7):
        parts = [shards.partition_files(files, i, total) for i in range(total)]
        flat = [f for part in parts for f in part]
        assert sorted(flat) == sorted(files)
        assert len(flat) == len(set(flat))


def test_partition_is_stable_when_files_are_added() -> None:
    files = [f"tests/test_{n}.py" for n in range(40)]
    before = {f: shards.shard_of(f, 4) for f in files}
    extra = files + ["tests/test_new.py"]
    assert all(shards.shard_of(f, 4) == before[f] for f in files)
    assert len(shards.partition_files(extra, 0, 4) + shards.partition_files(extra, 1, 4)
               + shards.partition_files(extra, 2, 4) + shards.partition_files(extra, 3, 4)) == len(extra)


def test_command_for_shard_replaces_target_and_honours_ignore(tmp_path: Path) -> None:
    (tmp_path / "tests" / "sub").mkdir(parents=True)
    for name in ("test_a.py", "test_b.py", "test_c.py", "sub/test_d.py", "test_skip.py"):
        (tmp_path / "tests" / name).write_text("", encoding="utf-8")
    cmd = ["python", "-m", "pytest", "tests", "-q", "--ignore=tests/test_skip.py"]
    seen: list[str] = []
    for spec in ("1/2", "2/2"):
        out = shards.command_for_shard(cmd, spec, tmp_path)
        assert out[:3] == cmd[:3] and "tests" not in out and "-q" in out
        seen += [t for t in out if t.startswith("tests/") and t.endswith(".py") and t != "tests/test_skip.py"]
    assert sorted(seen) == ["tests/sub/test_d.py", "tests/test_a.py", "tests/test_b.py", "tests/test_c.py"]
    compile_cmd = ["python", "-m", "compileall", "-q", "core"]
    assert shards.command_for_shard(compile_cmd, "1/2", tmp_path) == compile_cmd


@pytest.mark.parametrize("spec", ["0/4", "5/4", "a/b", "1", "1/0"])
def test_parse_shard_rejects_bad_specs(spec: str) -> None:
    with pytest.raises(ValueError):
        shards.parse_shard(spec)


def test_parse_shard_is_one_based() -> None:
    assert shards.parse_shard("1/4") == (0, 4)
    assert shards.parse_shard("4/4") == (3, 4)


@pytest.mark.parametrize(
    "paths",
    [
        [".factory/demands/demands.json"],
        [".factory/demands/demands.json", ".factory/owner_actions/owner_actions.json", "docs/x/y.md"],
        ["README.md", "docs/proposals/a.patch"],
    ],
)
def test_light_paths(paths: list[str]) -> None:
    assert ci_paths.is_heavy(paths) is False


@pytest.mark.parametrize(
    "paths",
    [
        [],
        ["core/harness/runner.py"],
        [".github/workflows/ci.yml"],
        [".factory/config/line_routing.json"],
        [".factory/holdout/case.json"],
        ["AGENTS.md"],
        ["CLAUDE.md"],
        ["docs/../core/x.py"],
        ["skills/foo/SKILL.md"],
        [".factory/demands/demands.json", "tests/test_x.py"],
        ["harness.config.json"],
    ],
)
def test_heavy_paths(paths: list[str]) -> None:
    assert ci_paths.is_heavy(paths) is True
