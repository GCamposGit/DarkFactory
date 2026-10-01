"""Gate: nothing may be tracked by git AND matched by ``.gitignore`` (USR-102).

``.gitignore`` only protects files that were never added. A file that is already
tracked and later matches an ignore rule keeps being versioned (and published, in
this PUBLIC repository) with no warning: ``.factory/telegram/config.json`` and 71
reports under ``.factory/reports/`` are exactly that. The gate compares
``git ls-files -ci --exclude-standard`` with the versioned allowlist
``tests/data/tracked_ignored_allowlist.txt``:

* a tracked-and-ignored path that is NOT in the allowlist fails (untrack it with
  ``git rm --cached`` or fix the ignore rule; never grow the allowlist to pass);
* an allowlist path that is no longer tracked-and-ignored fails, so the allowlist
  can only shrink.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_FILE = Path(__file__).resolve().parent / "data" / "tracked_ignored_allowlist.txt"

GIT_TIMEOUT_SEC = 120.0
# An empty core.excludesFile neutralizes the developer's global ignore file, so the
# verdict depends only on the repository (.gitignore files and .git/info/exclude)
# and is the same on every machine and in CI.
_LS_FILES_ARGS = ("-c", "core.excludesFile=", "ls-files", "-ci", "--exclude-standard", "-z")

FIX_HINT = (
    "Desversione com `git rm --cached -- <caminho>` (o arquivo local permanece) ou ajuste a regra "
    "do .gitignore. Nao acrescente o caminho a tests/data/tracked_ignored_allowlist.txt para "
    "'fazer passar': aquela lista so pode encolher."
)


class AllowlistFormatError(ValueError):
    """The allowlist file is malformed (duplicate, absolute path, glob...)."""


@dataclass(frozen=True)
class ConsistencyReport:
    unexpected: tuple[str, ...]  # tracked and ignored, not allowlisted
    stale: tuple[str, ...]  # allowlisted, no longer tracked and ignored

    @property
    def ok(self) -> bool:
        return not self.unexpected and not self.stale


def parse_allowlist(text: str) -> list[str]:
    """One repo-relative POSIX path per line; ``#`` starts a comment; blank lines are skipped."""
    paths: list[str] = []
    seen: set[str] = set()
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "\\" in line or line.startswith(("/", "./")) or ".." in line.split("/"):
            raise AllowlistFormatError(f"linha {number}: use caminho relativo com separador '/': {line}")
        if any(ch in line for ch in "*?["):
            raise AllowlistFormatError(f"linha {number}: sem curingas, liste o caminho exato: {line}")
        if line in seen:
            raise AllowlistFormatError(f"linha {number}: caminho repetido: {line}")
        seen.add(line)
        paths.append(line)
    return paths


def load_allowlist(path: Path = ALLOWLIST_FILE) -> list[str]:
    return parse_allowlist(path.read_text(encoding="utf-8-sig"))


def tracked_but_ignored(repo: Path) -> list[str]:
    """Paths in the index that an ignore rule also matches (``git ls-files -ci``), sorted."""
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    result = subprocess.run(
        ["git", *_LS_FILES_ARGS],
        cwd=str(repo),
        env=env,
        capture_output=True,
        timeout=GIT_TIMEOUT_SEC,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git ls-files falhou (rc={result.returncode}): {detail}")
    return sorted(p for p in result.stdout.decode("utf-8", errors="surrogateescape").split("\0") if p)


def check_consistency(repo: Path, allowlist: list[str]) -> ConsistencyReport:
    current = set(tracked_but_ignored(repo))
    allowed = set(allowlist)
    return ConsistencyReport(
        unexpected=tuple(sorted(current - allowed)),
        stale=tuple(sorted(allowed - current)),
    )


def _render(paths: tuple[str, ...]) -> str:
    return "\n".join(f"  - {p}" for p in paths)


# --- the real repository ----------------------------------------------------


@pytest.fixture(scope="module")
def repo_report() -> ConsistencyReport:
    try:
        return check_consistency(REPO_ROOT, load_allowlist())
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"checkout sem git utilizavel: {exc}")


def test_allowlist_file_is_well_formed() -> None:
    assert ALLOWLIST_FILE.is_file()
    assert load_allowlist(), "a allowlist nao pode estar vazia enquanto USR-100/USR-102 estiverem abertos"


def test_no_tracked_file_is_ignored_outside_the_allowlist(repo_report: ConsistencyReport) -> None:
    assert not repo_report.unexpected, (
        "arquivos rastreados que o .gitignore tambem cobre (seriam versionados e publicados em silencio):\n"
        + _render(repo_report.unexpected)
        + "\n"
        + FIX_HINT
    )


def test_allowlist_only_shrinks(repo_report: ConsistencyReport) -> None:
    assert not repo_report.stale, (
        "caminhos da allowlist que nao sao mais rastreados-e-ignorados (remova as linhas de "
        "tests/data/tracked_ignored_allowlist.txt):\n" + _render(repo_report.stale)
    )


# --- allowlist parser ---------------------------------------------------------


def test_parse_allowlist_skips_comments_and_blank_lines_and_accepts_crlf() -> None:
    text = "# grupo\r\n\r\n.factory/reports/a.md\r\n   \r\n  .factory/telegram/config.json  \r\n# fim\r\n"
    assert parse_allowlist(text) == [".factory/reports/a.md", ".factory/telegram/config.json"]


@pytest.mark.parametrize(
    ("line", "fragment"),
    [
        (".factory\\reports\\a.md", "separador"),
        ("/etc/passwd", "separador"),
        ("./a.md", "separador"),
        ("a/../b.md", "separador"),
        (".factory/reports/*", "curingas"),
        ("reports/[ab].md", "curingas"),
    ],
)
def test_parse_allowlist_rejects_malformed_lines(line: str, fragment: str) -> None:
    with pytest.raises(AllowlistFormatError, match=fragment):
        parse_allowlist(line + "\n")


def test_parse_allowlist_rejects_duplicates() -> None:
    with pytest.raises(AllowlistFormatError, match="repetido"):
        parse_allowlist("a.md\nb.md\na.md\n")


# --- mutation tests on throwaway repositories --------------------------------


def _git(repo: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


@pytest.fixture
def temp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "DarkFac Test")
    _git(repo, "config", "user.email", "test@darkfac.internal")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _write(repo: Path, rel_path: str, content: str = "x\n") -> None:
    target = repo / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="\n")


def _track(repo: Path, rel_path: str, content: str = "x\n") -> None:
    _write(repo, rel_path, content)
    _git(repo, "add", "--", rel_path)


def _force_track(repo: Path, *rel_paths: str) -> None:
    """Write and `git add -f` files that an ignore rule matches (how they get tracked anyway)."""
    for rel_path in rel_paths:
        _write(repo, rel_path)
    _git(repo, "add", "-f", "--", *rel_paths)


def test_clean_repository_is_consistent(temp_repo: Path) -> None:
    _track(temp_repo, ".gitignore", "build/\n")
    _track(temp_repo, "src/app.py")

    assert check_consistency(temp_repo, []).ok


def test_new_ignored_untracked_file_is_fine(temp_repo: Path) -> None:
    _track(temp_repo, ".gitignore", "build/\n*.log\n")
    _write(temp_repo, "build/output.bin")
    _write(temp_repo, "debug.log")

    assert tracked_but_ignored(temp_repo) == []
    assert check_consistency(temp_repo, []).ok


def test_tracked_file_that_becomes_ignored_fails(temp_repo: Path) -> None:
    _track(temp_repo, "reports/summary.md")
    _track(temp_repo, "src/app.py")
    assert check_consistency(temp_repo, []).ok

    _track(temp_repo, ".gitignore", "reports/\n")
    report = check_consistency(temp_repo, [])

    assert not report.ok
    assert report.unexpected == ("reports/summary.md",)
    assert report.stale == ()


def test_force_added_ignored_file_fails(temp_repo: Path) -> None:
    _track(temp_repo, ".gitignore", "secrets/*.json\n")
    _force_track(temp_repo, "secrets/token.json")

    assert check_consistency(temp_repo, []).unexpected == ("secrets/token.json",)


def test_listed_path_is_accepted(temp_repo: Path) -> None:
    _track(temp_repo, ".gitignore", "reports/\n")
    _force_track(temp_repo, "reports/summary.md")

    assert check_consistency(temp_repo, ["reports/summary.md"]).ok


def test_allowlist_only_covers_the_listed_paths(temp_repo: Path) -> None:
    _track(temp_repo, ".gitignore", "reports/\n")
    _force_track(temp_repo, "reports/old.md", "reports/new.md")

    report = check_consistency(temp_repo, ["reports/old.md"])

    assert report.unexpected == ("reports/new.md",)
    assert report.stale == ()


def test_stale_entry_after_untracking_fails(temp_repo: Path) -> None:
    _track(temp_repo, ".gitignore", "reports/\n")
    _force_track(temp_repo, "reports/summary.md")
    allowlist = ["reports/summary.md"]
    assert check_consistency(temp_repo, allowlist).ok

    _git(temp_repo, "rm", "--cached", "--quiet", "--", "reports/summary.md")
    report = check_consistency(temp_repo, allowlist)

    assert not report.ok
    assert report.unexpected == ()
    assert report.stale == ("reports/summary.md",)


def test_stale_entry_after_the_ignore_rule_is_removed_fails(temp_repo: Path) -> None:
    _track(temp_repo, ".gitignore", "reports/\n")
    _force_track(temp_repo, "reports/summary.md")
    allowlist = ["reports/summary.md"]
    assert check_consistency(temp_repo, allowlist).ok

    _track(temp_repo, ".gitignore", "build/\n")
    report = check_consistency(temp_repo, allowlist)

    assert report.stale == ("reports/summary.md",)


def test_entry_for_a_path_that_never_existed_is_stale(temp_repo: Path) -> None:
    _track(temp_repo, "src/app.py")

    assert check_consistency(temp_repo, ["ghost/file.txt"]).stale == ("ghost/file.txt",)


def test_nested_gitignore_and_odd_file_names_are_handled(temp_repo: Path) -> None:
    _track(temp_repo, "docs/.gitignore", "*.draft\n")
    _force_track(temp_repo, "docs/rascunho com espaco-não-ascii.draft")

    assert tracked_but_ignored(temp_repo) == ["docs/rascunho com espaco-não-ascii.draft"]


def test_verdict_ignores_the_developers_global_excludes_file(temp_repo: Path, tmp_path: Path) -> None:
    global_excludes = tmp_path / "global_excludes"
    global_excludes.write_text("*.log\n", encoding="utf-8")
    _git(temp_repo, "config", "core.excludesFile", str(global_excludes))
    _force_track(temp_repo, "app.log")

    # Sanity: with the global file honored, git itself would call app.log ignored.
    plain = subprocess.run(
        ["git", "ls-files", "-ci", "--exclude-standard"],
        cwd=str(temp_repo),
        capture_output=True,
        text=True,
        check=True,
    )
    assert plain.stdout.split() == ["app.log"]
    assert tracked_but_ignored(temp_repo) == []
