"""Scanner of secrets in git-tracked files (USR-100).

The repository is public. ``.factory/telegram/config.json`` was committed with
a real Telegram bot token before anyone noticed, because nothing looked at the
*content* of tracked files: ``.gitignore`` only protects files that were never
added. This module is the reusable detector behind ``tests/test_no_tracked_secrets.py``
and, later, the pre-commit guard of ``core.git.autonomy``.

Design rules:

* **Pure and offline.** Only local ``git ls-files`` and file reads; never any
  network call and no import of the rest of ``core``.
* **A secret value is never exposed.** A :class:`Finding` carries the path, the
  line, the pattern name and the first :data:`MASK_VISIBLE_CHARS` characters
  followed by ``***``. The matched text is not stored anywhere, so it cannot
  leak through ``repr``, logs or assertion messages.
* **Allowlist = explicit, justified and shrinking.** Every
  :class:`AllowlistEntry` names a path, the exact patterns tolerated there and
  why. An entry that no longer matches anything is *stale* and reported, so
  the allowlist can only shrink once a file is cleaned or untracked.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("darkfac.git.secret_scan")

MASK_VISIBLE_CHARS = 3
MASK_SUFFIX = "***"
MAX_FILE_BYTES = 4 * 1024 * 1024
GIT_TIMEOUT_SEC = 120.0
_BINARY_SNIFF_BYTES = 8192

# Media, archives and compiled artifacts: never text a secret can hide in.
BINARY_EXTENSIONS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".icns", ".tif", ".tiff",
        ".mp3", ".mp4", ".m4a", ".wav", ".ogg", ".flac", ".mov", ".avi", ".mkv", ".webm",
        ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".tar",
        ".woff", ".woff2", ".ttf", ".otf", ".eot",
        ".docx", ".xlsx", ".pptx", ".odt",
        ".db", ".sqlite", ".sqlite3", ".pyc", ".pyd", ".so", ".dll", ".exe", ".bin",
        ".onnx", ".pt", ".parquet", ".npy",
    }
)  # fmt: skip

# Git variables that would redirect a subprocess to another repository when the
# scan runs from inside a hook.
_GIT_REDIRECT_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX")


@dataclass(frozen=True)
class SecretPattern:
    """A named detector. ``name`` is what reports and allowlists refer to.

    ``ignore`` receives a match and returns True when it is an obvious
    placeholder; it exists for the one pattern whose shape (a URL with a
    password) is also what documentation and tests legitimately contain.
    """

    name: str
    regex: re.Pattern[str]
    description: str
    ignore: Callable[[re.Match[str]], bool] | None = None


def _pattern(
    name: str,
    regex: str,
    description: str,
    ignore: Callable[[re.Match[str]], bool] | None = None,
) -> SecretPattern:
    return SecretPattern(name=name, regex=re.compile(regex), description=description, ignore=ignore)


# A URL password that says what it is ("secret", "changeme", "SENHA") or carries
# no entropy ("***", "xxx", "p") is documentation, not a credential.
_PLACEHOLDER_PASSWORD_MARKERS = (
    "secret", "pass", "pwd", "senha", "test", "fake", "dummy", "placeholder", "example",
    "changeme", "change-me", "sentinel", "redacted", "mock", "demo", "sample", "your",
)  # fmt: skip
_PLACEHOLDER_PASSWORD_FILLER = frozenset("*.x#_-")
_PLACEHOLDER_PASSWORD_MAX_LEN = 3


def _is_placeholder_password(match: re.Match[str]) -> bool:
    password = match.group("password").lower()
    return (
        len(password) <= _PLACEHOLDER_PASSWORD_MAX_LEN
        or set(password) <= _PLACEHOLDER_PASSWORD_FILLER
        or any(marker in password for marker in _PLACEHOLDER_PASSWORD_MARKERS)
    )


# Lengths follow the real key formats so that prose ("sk-learn", "ghp_" in a
# comment) and short placeholders ("sk-ant-xxxx") do not trigger a finding.
SECRET_PATTERNS: tuple[SecretPattern, ...] = (
    # No \b around the token: Bot API URLs glue it to "bot" ("/bot<id>:<hash>/") and
    # the hash may legitimately end in "-".
    _pattern("telegram_bot_token", r"(?<!\d)\d{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])", "Telegram bot token"),
    _pattern("anthropic_api_key", r"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{20,}", "Anthropic API key"),
    _pattern("openrouter_api_key", r"(?<![A-Za-z0-9_-])sk-or-v1-[A-Za-z0-9]{32,}", "OpenRouter API key"),
    _pattern(
        "openai_api_key",
        r"(?<![A-Za-z0-9_-])sk-(?!ant-|or-v1-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{32,}",
        "OpenAI API key",
    ),
    _pattern("xai_api_key", r"(?<![A-Za-z0-9_-])xai-[A-Za-z0-9]{32,}", "xAI API key"),
    _pattern(
        "github_token",
        r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})",
        "GitHub token",
    ),
    _pattern("google_api_key", r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}", "Google API key"),
    _pattern("aws_access_key_id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "AWS access key id"),
    _pattern(
        "private_key_block",
        r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----",
        "PEM private key block",
    ),
    _pattern(
        "postgres_url_with_password",
        # Templates (${VAR}, <value>) and regex literals ([^@]+) are not credentials.
        r"postgres(?:ql)?(?:\+[a-z0-9]+)?://[^:/\s'\"@<>${}\[\]\\^]+:(?P<password>[^@\s'\"<>${}\[\]\\^]+)@",
        "PostgreSQL URL with an inline password",
        ignore=_is_placeholder_password,
    ),
)
PATTERN_NAMES = frozenset(p.name for p in SECRET_PATTERNS)


@dataclass(frozen=True)
class Finding:
    """One match. Holds no secret: only a three-character masked prefix."""

    path: str
    pattern: str
    line: int
    masked: str

    def render(self) -> str:
        return f"{self.path}:{self.line} [{self.pattern}] {self.masked}"


@dataclass(frozen=True)
class AllowlistEntry:
    """A justified exception: ``patterns`` are tolerated in ``path`` and nowhere else.

    ``ticket`` is set for TEMPORARY entries that must disappear when the ticket
    lands; the staleness report then names it so the cleanup is not forgotten.
    """

    path: str
    patterns: tuple[str, ...]
    reason: str
    ticket: str = ""


@dataclass(frozen=True)
class ScanReport:
    """Outcome of a repository scan after the allowlist was applied."""

    violations: tuple[Finding, ...]
    stale: tuple[str, ...]
    scanned_files: int

    @property
    def ok(self) -> bool:
        return not self.violations and not self.stale

    def render(self) -> str:
        lines: list[str] = []
        if self.violations:
            lines.append(
                "segredos potenciais em arquivos rastreados (caminho:linha [padrao] 3 primeiros "
                "caracteres; o valor nunca e exibido):"
            )
            lines.extend(f"  - {finding.render()}" for finding in self.violations)
        if self.stale:
            lines.append("entradas obsoletas na allowlist de segredos (remova-as):")
            lines.extend(f"  - {message}" for message in self.stale)
        return "\n".join(lines)


def mask(value: str) -> str:
    """First :data:`MASK_VISIBLE_CHARS` characters plus a fixed suffix (length not leaked)."""
    return value[:MASK_VISIBLE_CHARS] + MASK_SUFFIX


def scan_text(
    path: str,
    text: str,
    patterns: Sequence[SecretPattern] = SECRET_PATTERNS,
) -> list[Finding]:
    """Match every pattern against ``text``; ``path`` is only used to label findings."""
    findings: list[Finding] = []
    for pattern in patterns:
        for match in pattern.regex.finditer(text):
            if pattern.ignore is not None and pattern.ignore(match):
                continue
            line = text.count("\n", 0, match.start()) + 1
            findings.append(Finding(path=path, pattern=pattern.name, line=line, masked=mask(match.group(0))))
    findings.sort(key=lambda f: (f.line, f.pattern))
    return findings


def _git(repo: Path, *args: str) -> bytes:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REDIRECT_VARS}
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        timeout=GIT_TIMEOUT_SEC,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} falhou (rc={result.returncode}): {detail}")
    return result.stdout


def list_tracked_files(repo: Path) -> list[str]:
    """Tracked paths (POSIX separators) from ``git ls-files -z``, sorted."""
    raw = _git(repo, "ls-files", "-z")
    return sorted(p for p in raw.decode("utf-8", errors="surrogateescape").split("\0") if p)


def _is_scannable(repo: Path, rel_path: str, max_bytes: int) -> bool:
    if Path(rel_path).suffix.lower() in BINARY_EXTENSIONS:
        return False
    full = repo / rel_path
    # Symlinks, submodules (directories) and files deleted from the worktree
    # have no content of their own to read.
    if full.is_symlink() or not full.is_file():
        return False
    try:
        return full.stat().st_size <= max_bytes
    except OSError:
        return False


def scan_file(
    repo: Path,
    rel_path: str,
    *,
    patterns: Sequence[SecretPattern] = SECRET_PATTERNS,
    max_bytes: int = MAX_FILE_BYTES,
) -> list[Finding]:
    """Scan one worktree file; binary, oversized or unreadable files yield nothing."""
    if not _is_scannable(repo, rel_path, max_bytes):
        return []
    try:
        data = (repo / rel_path).read_bytes()
    except OSError:
        logger.warning("secret_scan: nao foi possivel ler %s", rel_path)
        return []
    if b"\0" in data[:_BINARY_SNIFF_BYTES]:
        return []
    return scan_text(rel_path, data.decode("utf-8", errors="replace"), patterns)


def scan_paths(
    repo: Path,
    paths: Iterable[str],
    *,
    patterns: Sequence[SecretPattern] = SECRET_PATTERNS,
    max_bytes: int = MAX_FILE_BYTES,
) -> list[Finding]:
    """Scan the given repo-relative paths (for example the files of a staged commit)."""
    findings: list[Finding] = []
    for rel_path in paths:
        findings.extend(scan_file(repo, rel_path, patterns=patterns, max_bytes=max_bytes))
    return findings


def apply_allowlist(findings: Iterable[Finding], allowlist: Collection[AllowlistEntry]) -> list[Finding]:
    """Findings not covered by an entry for the same path *and* pattern."""
    allowed = {(entry.path, name) for entry in allowlist for name in entry.patterns}
    return [f for f in findings if (f.path, f.pattern) not in allowed]


def find_stale_entries(
    findings: Iterable[Finding],
    allowlist: Collection[AllowlistEntry],
    tracked: Collection[str],
) -> list[str]:
    """Allowlist problems that must be fixed by editing the allowlist, not the code.

    Reported: unknown pattern names, entries for paths that are no longer
    tracked, and entries whose pattern no longer matches in the file.
    """
    tracked_set = set(tracked)
    matched = {(f.path, f.pattern) for f in findings}
    stale: list[str] = []
    seen_paths: set[str] = set()
    for entry in allowlist:
        suffix = f" (entrada temporaria do {entry.ticket}: remova-a)" if entry.ticket else ""
        if entry.path in seen_paths:
            stale.append(f"{entry.path}: entrada duplicada na allowlist")
        seen_paths.add(entry.path)
        unknown = sorted(set(entry.patterns) - PATTERN_NAMES)
        if unknown:
            stale.append(f"{entry.path}: padrao desconhecido {', '.join(unknown)}")
        if entry.path not in tracked_set:
            stale.append(f"{entry.path}: arquivo nao esta mais rastreado{suffix}")
            continue
        for name in entry.patterns:
            if name in PATTERN_NAMES and (entry.path, name) not in matched:
                stale.append(f"{entry.path}: padrao {name} nao ocorre mais no arquivo{suffix}")
    return stale


def scan_repository(
    repo: Path,
    allowlist: Collection[AllowlistEntry] | None = None,
    *,
    patterns: Sequence[SecretPattern] = SECRET_PATTERNS,
    max_bytes: int = MAX_FILE_BYTES,
) -> ScanReport:
    """Scan every tracked file of ``repo`` and apply ``allowlist`` (default: :data:`ALLOWLIST`)."""
    entries = ALLOWLIST if allowlist is None else allowlist
    tracked = list_tracked_files(repo)
    findings = scan_paths(repo, tracked, patterns=patterns, max_bytes=max_bytes)
    return ScanReport(
        violations=tuple(apply_allowlist(findings, entries)),
        stale=tuple(find_stale_entries(findings, entries, tracked)),
        scanned_files=len(tracked),
    )


_TELEGRAM = "telegram_bot_token"
_ANTHROPIC = "anthropic_api_key"
_OPENAI = "openai_api_key"
_GITHUB = "github_token"
_POSTGRES = "postgres_url_with_password"

# Every entry says WHY the match is acceptable. To add one: prefer rewriting the
# fixture so it no longer looks like a credential; if the shape is the point of
# the test (redaction, probes), list the path, the exact pattern and the reason.
ALLOWLIST: tuple[AllowlistEntry, ...] = (
    # --- TEMPORARY (USR-100): the real Telegram bot token is committed. ----------
    # Rotation is the owner's action. Untracking the file comes after it, because
    # the Desktop/Notebook nodes read the local copy and a `git pull` would delete
    # it. (The same value was also pasted into two HF-15 fixtures; they now build a
    # FAKE token at runtime and their entries were removed.) When a file is
    # untracked or cleaned, its entry turns stale and the gate fails until the
    # entry is removed.
    AllowlistEntry(
        path=".factory/telegram/config.json",
        patterns=(_TELEGRAM,),
        reason="token de bot real exposto no repositorio publico (commit 0c7514e); desversionar apos a rotacao",
        ticket="USR-100",
    ),
    # --- Sentinelas de teste: o formato E o ponto (redacao, probes, validacao). --
    AllowlistEntry(
        path="tests/line/test_agent_failure_visibility.py",
        patterns=(_ANTHROPIC,),
        reason="sentinela que prova a redacao de chaves na visibilidade de falhas do agente",
    ),
    AllowlistEntry(
        path="tests/line/test_agent_retry.py",
        patterns=(_ANTHROPIC,),
        reason="sentinela que prova a redacao de chaves no diagnostico de retry do agente",
    ),
    AllowlistEntry(
        path="tests/line/test_claude_probe_hardening.py",
        patterns=(_ANTHROPIC,),
        reason="sentinela que prova que o probe do Claude nao vaza a chave no resultado",
    ),
    AllowlistEntry(
        path="tests/test_continuous_observer.py",
        patterns=(_GITHUB,),
        reason="sentinela de token do GitHub para provar a redacao do observador continuo",
    ),
    AllowlistEntry(
        path="tests/test_dokploy_redeploy.py",
        patterns=(_OPENAI,),
        reason="sentinela de chave marcada como 'leak' para provar que o redeploy nao a imprime",
    ),
    AllowlistEntry(
        path="tests/test_hf14_telegram_n8n_integration.py",
        patterns=(_GITHUB,),
        reason="sentinela de token do GitHub para provar a redacao na integracao Telegram/n8n",
    ),
    AllowlistEntry(
        path="tests/test_integration_handler.py",
        patterns=(_ANTHROPIC, _GITHUB),
        reason="sentinelas de redacao do handler de integracao (chave Anthropic e tokens GitHub)",
    ),
    AllowlistEntry(
        path="tests/test_postgres_hybrid_persistence.py",
        patterns=(_POSTGRES,),
        reason="URLs 'mock_url'/'superuser_url' do teste de persistencia hibrida; senhas fake, host .internal",
    ),
    AllowlistEntry(
        path="tests/test_reusable_pilots.py",
        patterns=(_OPENAI,),
        reason="sentinela de chave para provar que os pilotos reutilizaveis nao a persistem",
    ),
    AllowlistEntry(
        path="tests/test_workflow_verification.py",
        patterns=(_POSTGRES,),
        reason="rota 'postgres://user:...@host/db' de fixture; usuario e host genericos",
    ),
)
