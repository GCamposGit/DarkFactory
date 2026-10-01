"""Gate: no secret may live in a git-tracked file of this PUBLIC repository (USR-100).

``.gitignore`` only protects files that were never added; a real Telegram bot
token reached the public history because nothing inspected tracked *content*.
This gate scans ``git ls-files`` with the detectors of ``core.git.secret_scan``.

Rules the tests enforce:

* a finding is reported as ``path:line [pattern] abc***`` -- the secret value is
  never printed, logged or stored;
* the allowlist is explicit and justified, and it can only shrink: an entry that
  no longer matches (file untracked or cleaned) fails the gate so it gets removed;
* every fake credential below is assembled at runtime, so this file contains no
  literal that the scanner would flag.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from core.git import secret_scan as scan

REPO_ROOT = Path(__file__).resolve().parents[1]

TELEGRAM = "telegram_bot_token"
GITHUB = "github_token"

# Well-formed FAKE credentials, one per pattern, built from fragments.
FAKE_SECRETS: dict[str, str] = {
    "telegram_bot_token": "123456789" + ":" + "Ab1_-" * 7,
    "anthropic_api_key": "sk-" + "ant-" + "api03-" + "Ab1" * 10,
    "openrouter_api_key": "sk-" + "or-v1-" + "a1b2" * 10,
    "openai_api_key": "sk-" + "proj-" + "Ab1" * 14,
    "xai_api_key": "xai-" + "Ab1" * 14,
    "github_token": "ghp" + "_" + "Ab1" * 14,
    "google_api_key": "AIza" + "Ab1_-" * 7,
    "aws_access_key_id": "AKIA" + "ABCD1234EFGH5678",
    "private_key_block": "-----BEGIN " + "RSA " + "PRIVATE KEY-----",
    "postgres_url_with_password": "postgresql://" + "svc_user:" + "Xk3vQ9zLp2Rt" + "@db.internal:5432/app",
}

# Shapes that resemble a credential but are prose, templates or placeholders.
HARMLESS_TEXTS: dict[str, str] = {
    "short telegram-like pair": "id 12345:abcdef is not a bot token",
    "anthropic prefix without key": "export ANTHROPIC_API_KEY=" + "sk-" + "ant-xxxx",
    "scikit name": "pip install " + "sk-" + "learn-extra",
    "github prefix in prose": "tokens start with " + "ghp" + "_ and are 40 chars long",
    "postgres template": "postgresql://" + "user:${DB_PASSWORD}" + "@db:5432/app",
    "postgres angle placeholder": "postgresql://" + "user:<password>" + "@db:5432/app",
    "postgres obvious placeholder": "postgresql://" + "user:" + "changeme" + "@db:5432/app",
    "postgres masked": "postgresql://" + "user:" + "*" * 8 + "@db:5432/app",
    "postgres regex literal": r"postgresql://[^:]+:[^@]+@[^/]+/\w+",
    "private key mention": "never commit a PRIVATE KEY file",
}


def test_every_pattern_has_a_fake_credential() -> None:
    assert set(FAKE_SECRETS) == scan.PATTERN_NAMES


@pytest.mark.parametrize("pattern_name", sorted(FAKE_SECRETS))
def test_each_pattern_detects_its_fake_credential(pattern_name: str) -> None:
    value = FAKE_SECRETS[pattern_name]
    findings = scan.scan_text("sample.txt", f"first line\nkey = {value}\n")
    assert [(f.pattern, f.line) for f in findings] == [(pattern_name, 2)]


@pytest.mark.parametrize(
    "template",
    [
        "https://api.telegram.org/bot{value}/getMe",
        '{{"bot_token": "{value}"}}',
        "TELEGRAM_BOT_TOKEN={value}",
        "token={value}&chat_id=1",
    ],
)
def test_telegram_token_is_found_inside_urls_and_assignments(template: str) -> None:
    findings = scan.scan_text("notes.txt", template.format(value=FAKE_SECRETS[TELEGRAM]))
    assert [f.pattern for f in findings] == [TELEGRAM]


@pytest.mark.parametrize("label", sorted(HARMLESS_TEXTS))
def test_prose_templates_and_placeholders_are_not_findings(label: str) -> None:
    assert scan.scan_text("doc.md", HARMLESS_TEXTS[label]) == []


def test_mask_keeps_three_characters_and_hides_the_length() -> None:
    assert scan.mask("abcdefghij") == "abc***"
    assert scan.mask("abcdefghijklmnopqrstuvwxyz") == "abc***"
    assert scan.mask("ab") == "ab***"


def test_finding_never_carries_the_matched_value() -> None:
    value = FAKE_SECRETS[TELEGRAM]
    (finding,) = scan.scan_text("config.json", f'{{"bot_token": "{value}"}}')
    assert finding.masked == "123***"
    assert value not in repr(finding)
    assert value not in finding.render()
    assert value[4:] not in repr(finding) + finding.render()


# --- the real repository ----------------------------------------------------


@pytest.fixture(scope="module")
def repo_report() -> scan.ScanReport:
    try:
        scan.list_tracked_files(REPO_ROOT)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"checkout sem git utilizavel: {exc}")
    return scan.scan_repository(REPO_ROOT)


def test_no_tracked_file_contains_a_secret_outside_the_allowlist(repo_report: scan.ScanReport) -> None:
    assert repo_report.scanned_files > 0
    assert not repo_report.violations, (
        "\n"
        + repo_report.render()
        + "\nRemova o segredo do arquivo (e rotacione-o se for real). So se for uma sentinela de teste "
        "inofensiva, adicione uma AllowlistEntry justificada em core/git/secret_scan.py."
    )


def test_allowlist_has_no_stale_entries(repo_report: scan.ScanReport) -> None:
    assert not repo_report.stale, "\n" + repo_report.render()


def test_allowlist_entries_are_well_formed_and_justified() -> None:
    paths = [entry.path for entry in scan.ALLOWLIST]
    assert len(paths) == len(set(paths)), "caminho repetido na allowlist de segredos"
    for entry in scan.ALLOWLIST:
        assert entry.reason.strip(), f"{entry.path}: toda entrada precisa de motivo"
        assert entry.patterns, f"{entry.path}: liste os padroes tolerados"
        assert set(entry.patterns) <= scan.PATTERN_NAMES, f"{entry.path}: padrao desconhecido"
        assert "\\" not in entry.path and not entry.path.startswith("/"), (
            f"{entry.path}: use caminho relativo com separador '/'"
        )
        assert not any(ch in entry.path for ch in "*?["), f"{entry.path}: sem curingas na allowlist"


def test_the_exposed_telegram_token_entry_is_explicit_temporary_and_tied_to_usr_100() -> None:
    (entry,) = [e for e in scan.ALLOWLIST if e.path == ".factory/telegram/config.json"]
    assert entry.patterns == (TELEGRAM,)
    assert entry.ticket == "USR-100"
    assert "token" in entry.reason


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


def _track(repo: Path, rel_path: str, content: str | bytes) -> None:
    target = repo / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8", newline="\n")
    _git(repo, "add", "--", rel_path)


def test_clean_repository_passes(temp_repo: Path) -> None:
    _track(temp_repo, "README.md", "# nothing to see\n")
    report = scan.scan_repository(temp_repo, allowlist=())
    assert report.ok
    assert report.scanned_files == 1


def test_fake_telegram_token_in_a_tracked_file_is_reported_without_its_value(temp_repo: Path) -> None:
    value = FAKE_SECRETS[TELEGRAM]
    _track(temp_repo, "README.md", "# docs\n")
    _track(temp_repo, "config/bot.json", f'{{\n  "bot_token": "{value}"\n}}\n')

    report = scan.scan_repository(temp_repo, allowlist=())

    assert not report.ok
    assert [(f.path, f.pattern, f.line, f.masked) for f in report.violations] == [
        ("config/bot.json", TELEGRAM, 2, "123***")
    ]
    exposed = report.render() + repr(report)
    assert "config/bot.json:2 [telegram_bot_token] 123***" in exposed
    assert value not in exposed
    assert value[4:] not in exposed


def test_allowlisted_sentinel_is_not_reported(temp_repo: Path) -> None:
    _track(temp_repo, "tests/test_redaction.py", f'SENTINEL = "{FAKE_SECRETS[GITHUB]}"\n')
    allowlist = (
        scan.AllowlistEntry(
            path="tests/test_redaction.py", patterns=(GITHUB,), reason="sentinela de redacao"
        ),
    )

    report = scan.scan_repository(temp_repo, allowlist=allowlist)

    assert report.ok, report.render()


def test_allowlist_is_scoped_to_the_listed_pattern(temp_repo: Path) -> None:
    content = f'A = "{FAKE_SECRETS[GITHUB]}"\nB = "{FAKE_SECRETS[TELEGRAM]}"\n'
    _track(temp_repo, "tests/test_redaction.py", content)
    allowlist = (scan.AllowlistEntry(path="tests/test_redaction.py", patterns=(GITHUB,), reason="sentinela"),)

    report = scan.scan_repository(temp_repo, allowlist=allowlist)

    assert [(f.path, f.pattern) for f in report.violations] == [("tests/test_redaction.py", TELEGRAM)]
    assert not report.stale


def test_allowlist_is_scoped_to_the_listed_path(temp_repo: Path) -> None:
    _track(temp_repo, "tests/test_redaction.py", f'A = "{FAKE_SECRETS[GITHUB]}"\n')
    _track(temp_repo, "src/settings.py", f'TOKEN = "{FAKE_SECRETS[GITHUB]}"\n')
    allowlist = (scan.AllowlistEntry(path="tests/test_redaction.py", patterns=(GITHUB,), reason="sentinela"),)

    report = scan.scan_repository(temp_repo, allowlist=allowlist)

    assert [f.path for f in report.violations] == ["src/settings.py"]


def test_entry_whose_file_lost_the_secret_is_stale(temp_repo: Path) -> None:
    _track(temp_repo, "tests/test_redaction.py", "SENTINEL = 'cleaned up'\n")
    allowlist = (scan.AllowlistEntry(path="tests/test_redaction.py", patterns=(GITHUB,), reason="sentinela"),)

    report = scan.scan_repository(temp_repo, allowlist=allowlist)

    assert not report.violations
    assert len(report.stale) == 1
    assert "tests/test_redaction.py" in report.stale[0]
    assert GITHUB in report.stale[0]


def test_temporary_entry_goes_stale_when_the_file_is_untracked_and_names_the_ticket(temp_repo: Path) -> None:
    _track(temp_repo, "cfg/telegram.json", f'{{"bot_token": "{FAKE_SECRETS[TELEGRAM]}"}}\n')
    allowlist = (
        scan.AllowlistEntry(
            path="cfg/telegram.json", patterns=(TELEGRAM,), reason="token exposto", ticket="USR-100"
        ),
    )
    assert scan.scan_repository(temp_repo, allowlist=allowlist).ok

    _git(temp_repo, "rm", "--cached", "--quiet", "--", "cfg/telegram.json")
    report = scan.scan_repository(temp_repo, allowlist=allowlist)

    assert not report.violations
    assert len(report.stale) == 1
    assert "cfg/telegram.json" in report.stale[0]
    assert "USR-100" in report.stale[0]
    assert "nao esta mais rastreado" in report.stale[0]


def test_duplicate_and_unknown_pattern_entries_are_flagged(temp_repo: Path) -> None:
    _track(temp_repo, "a.txt", f"{FAKE_SECRETS[GITHUB]}\n")
    allowlist = (
        scan.AllowlistEntry(path="a.txt", patterns=(GITHUB,), reason="um"),
        scan.AllowlistEntry(path="a.txt", patterns=("not_a_pattern",), reason="dois"),
    )

    report = scan.scan_repository(temp_repo, allowlist=allowlist)

    assert any("duplicada" in message for message in report.stale)
    assert any("desconhecido" in message for message in report.stale)


def test_binary_oversized_and_media_files_are_skipped(temp_repo: Path) -> None:
    value = FAKE_SECRETS[TELEGRAM]
    _track(temp_repo, "assets/logo.png", f"{value}\n")
    _track(temp_repo, "data/blob.dat", b"\x00\x01\x02" + value.encode("ascii"))
    _track(temp_repo, "data/huge.txt", f"{value}\n" + "x" * 2048)

    report_default = scan.scan_repository(temp_repo, allowlist=())
    report_tiny_limit = scan.scan_repository(temp_repo, allowlist=(), max_bytes=1024)

    assert [f.path for f in report_default.violations] == ["data/huge.txt"]
    assert report_tiny_limit.ok


def test_findings_use_forward_slashes_and_survive_odd_file_names(temp_repo: Path) -> None:
    _track(temp_repo, "dir with space/arquivo-não-ascii.txt", f"{FAKE_SECRETS['google_api_key']}\n")

    report = scan.scan_repository(temp_repo, allowlist=())

    assert [f.path for f in report.violations] == ["dir with space/arquivo-não-ascii.txt"]


def test_files_deleted_from_the_worktree_are_ignored(temp_repo: Path) -> None:
    _track(temp_repo, "gone.txt", f"{FAKE_SECRETS[GITHUB]}\n")
    (temp_repo / "gone.txt").unlink()

    assert scan.scan_repository(temp_repo, allowlist=()).ok


def test_scan_paths_can_check_only_the_given_files(temp_repo: Path) -> None:
    _track(temp_repo, "a.txt", f"{FAKE_SECRETS[GITHUB]}\n")
    _track(temp_repo, "b.txt", "clean\n")

    assert scan.scan_paths(temp_repo, ["b.txt"]) == []
    assert [f.path for f in scan.scan_paths(temp_repo, ["a.txt", "b.txt"])] == ["a.txt"]
