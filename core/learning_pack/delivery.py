"""Learning pack da entrega: gerado do diff, dos testes e do ADR, com afirmacoes verificadas.

Entrada unica: ``build_for_delivery``. Toda afirmacao que cita caminho de arquivo
ou simbolo (entre crases) e conferida contra o diff e a arvore do head; a que
contradiz os fatos e removida e registrada em ``rejected_claims``.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

LlmFn = Callable[[dict[str, Any]], dict[str, str]]

_PATH_RE = re.compile(r"(?<![\w/.])((?:[\w.-]+/)*[\w.-]+\.[A-Za-z]{1,5})(?![\w/])")
_TICK_RE = re.compile(r"`([^`\n]+)`")
_SYMBOL_RE = re.compile(r"^[A-Za-z_][\w.]*$")
_DEF_RE = re.compile(r"^\+\s*(?:async\s+)?(?:def|class)\s+(\w+)")
_TEST_RE = re.compile(r"^\+\s*(?:async\s+)?def\s+(test_\w+)")
_DEF_RAW_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+(\w+)")
_TEST_RAW_RE = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)")
_CHANGED_WORDS = re.compile(
    r"\b(alterad|alteramos|modificad|editad|changed|modified|edited|updated|atualizad)", re.I
)
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


class FileChange(BaseModel):
    path: str
    added: int = 0
    removed: int = 0


class RejectedClaim(BaseModel):
    level: str
    claim: str
    reason: str


class LearningPackArtifact(BaseModel):
    ticket: str
    base_ref: str
    head_ref: str
    files: list[FileChange] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    tests_changed: list[str] = Field(default_factory=list)
    test_summary: str | None = None
    adr_path: str | None = None
    layman: str
    dev: str
    rejected_claims: list[RejectedClaim] = Field(default_factory=list)

    def to_markdown(self) -> str:
        return f"# Learning pack {self.ticket}\n\n## Leigo\n\n{self.layman}\n\n## Dev\n\n{self.dev}\n"


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} falhou: {res.stderr.strip()}")
    return res.stdout


def _resolve_sha(repo: Path, ref: str) -> str | None:
    res = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if res.returncode != 0:
        return None
    return res.stdout.strip()


def _targets_working_tree(repo: Path, head: str) -> bool:
    """True quando ``head`` resolve para o HEAD atual do repo (arvore de trabalho entra na coleta)."""
    head_sha = _resolve_sha(repo, head)
    current_sha = _resolve_sha(repo, "HEAD")
    return head_sha is not None and current_sha is not None and head_sha == current_sha


def _numstat_untracked(repo: Path, rel_path: str) -> tuple[int, int]:
    res = subprocess.run(
        ["git", "diff", "--no-index", "--numstat", "/dev/null", rel_path],
        cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if res.returncode not in (0, 1):
        raise RuntimeError(f"git diff --no-index falhou para {rel_path}: {res.stderr.strip()}")
    parts = res.stdout.strip().split("\t")
    if len(parts) == 3:
        a, r, _p = parts
        return (int(a) if a.isdigit() else 0, int(r) if r.isdigit() else 0)
    return (0, 0)


def _scan_untracked_symbols_tests(repo: Path, rel_path: str, symbols: list[str], tests: list[str]) -> None:
    try:
        text = (repo / rel_path).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return
    for line in text.splitlines():
        m = _TEST_RAW_RE.match(line)
        if m:
            tests.append(m.group(1))
            continue
        m = _DEF_RAW_RE.match(line)
        if m and m.group(1) not in symbols:
            symbols.append(m.group(1))


def _collect(repo: Path, base: str, head: str) -> tuple[list[FileChange], list[str], list[str]]:
    """Coleta base..head e, no HEAD atual, tambem staged, unstaged e untracked.

    Cada diff e lido separadamente: comparar so a arvore final esconderia mudancas que se anulam.
    """
    working_tree = _targets_working_tree(repo, head)
    diffs: list[tuple[str, ...]] = [(base, head)]
    if working_tree:
        diffs += [("--cached",), ()]
    changes: dict[str, FileChange] = {}
    symbols: list[str] = []
    tests: list[str] = []

    def add_change(path: str, added: int, removed: int) -> None:
        change = changes.setdefault(path, FileChange(path=path))
        change.added += added
        change.removed += removed

    for refs in diffs:
        for line in _git(repo, "diff", "--numstat", *refs).splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                a, r, p = parts
                add_change(p, int(a) if a.isdigit() else 0, int(r) if r.isdigit() else 0)
        for line in _git(repo, "diff", "-U0", *refs).splitlines():
            m = _TEST_RE.match(line)
            if m:
                tests.append(m.group(1))
            m = _DEF_RE.match(line)
            if m and m.group(1) not in symbols:
                symbols.append(m.group(1))
    if working_tree:
        for rel in _git(repo, "ls-files", "--others", "--exclude-standard").splitlines():
            rel = rel.strip()
            if not rel:
                continue
            add_change(rel, *_numstat_untracked(repo, rel))
            _scan_untracked_symbols_tests(repo, rel, symbols, tests)
    return list(changes.values()), symbols, sorted(set(tests))


def _symbol_exists(repo: Path, head: str, symbol: str) -> bool:
    """Procura na arvore do head; no HEAD atual, tambem no indice e na worktree (com untracked)."""
    last = symbol.split(".")[-1]
    searches: list[list[str]] = [["-qwF", last, head]]
    if _targets_working_tree(repo, head):
        searches += [["--cached", "-qwF", last], ["--untracked", "-qwF", last]]
    return any(
        subprocess.run(["git", "grep", *args], cwd=repo, capture_output=True).returncode == 0
        for args in searches
    )


def verify_text(
    text: str, level: str, repo: Path, head: str, changed: set[str]
) -> tuple[str, list[RejectedClaim]]:
    """Remove frases com afirmacoes que contradizem o diff/arvore; devolve texto limpo e rejeicoes."""
    tree = set(_git(repo, "ls-tree", "-r", "--name-only", head).splitlines())
    kept: list[str] = []
    rejected: list[RejectedClaim] = []
    for sent in (s.strip() for s in _SENT_SPLIT.split(text)):
        if not sent:
            continue
        reason = None
        paths = [p for p in _PATH_RE.findall(sent.replace("`", " ")) if "/" in p or p in tree]
        for p in paths:
            if p not in tree and p not in changed:
                reason = f"caminho inexistente: {p}"
            elif _CHANGED_WORDS.search(sent) and p not in changed:
                reason = f"arquivo nao alterado citado como alterado: {p}"
            if reason:
                break
        if not reason:
            for t in _TICK_RE.findall(sent):
                if _SYMBOL_RE.match(t) and t not in tree and not _symbol_exists(repo, head, t):
                    reason = f"simbolo inexistente: {t}"
                    break
        if reason:
            rejected.append(RejectedClaim(level=level, claim=sent, reason=reason))
        else:
            kept.append(sent)
    return " ".join(kept), rejected


def _deterministic(
    ticket: str, files: list[FileChange], symbols: list[str], tests: list[str],
    test_summary: str | None, adr: str | None,
) -> dict[str, str]:
    add = sum(f.added for f in files)
    rem = sum(f.removed for f in files)
    layman = (
        f"A entrega {ticket} mudou {len(files)} arquivo(s) do projeto "
        f"({add} linhas adicionadas, {rem} removidas) e vem com {len(tests)} teste(s) novo(s) ou alterado(s)."
    )
    dev_lines = [f"Arquivos alterados ({len(files)}):"]
    dev_lines += [f"- `{f.path}` (+{f.added}/-{f.removed})" for f in files]
    if symbols:
        dev_lines.append("Funcoes e classes tocadas: " + ", ".join(f"`{s}`" for s in symbols[:30]) + ".")
    if tests:
        dev_lines.append("Testes: " + ", ".join(f"`{t}`" for t in tests[:30]) + ".")
    if test_summary:
        dev_lines.append(f"Resultado dos testes: {test_summary}")
    if adr:
        dev_lines.append(f"ADR: `{adr}`.")
    return {"layman": layman, "dev": "\n".join(dev_lines)}


def build_for_delivery(
    repo: Path | str,
    base_ref: str,
    head_ref: str,
    ticket: str,
    test_summary: str | None = None,
    adr_path: str | None = None,
    llm: LlmFn | None = None,
) -> LearningPackArtifact:
    """Gera o pack leigo+dev a partir do diff base..head; afirmacoes do llm sao conferidas."""
    repo = Path(repo)
    files, symbols, tests = _collect(repo, base_ref, head_ref)
    changed = {f.path for f in files}
    base_text = _deterministic(ticket, files, symbols, tests, test_summary, adr_path)
    texts = dict(base_text)
    if llm is not None:
        facts = {
            "ticket": ticket,
            "files": [f.model_dump() for f in files],
            "symbols": symbols,
            "tests_changed": tests,
            "test_summary": test_summary,
            "adr_path": adr_path,
        }
        out = llm(facts) or {}
        texts = {k: out.get(k) or base_text[k] for k in ("layman", "dev")}
    rejected: list[RejectedClaim] = []
    for level in ("layman", "dev"):
        clean, rej = verify_text(texts[level], level, repo, head_ref, changed)
        rejected += rej
        texts[level] = clean or base_text[level]  # nunca entregar nivel vazio
    return LearningPackArtifact(
        ticket=ticket, base_ref=base_ref, head_ref=head_ref, files=files, symbols=symbols,
        tests_changed=tests, test_summary=test_summary, adr_path=adr_path,
        layman=texts["layman"], dev=texts["dev"], rejected_claims=rejected,
    )


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="python -m core.learning_pack.delivery")
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--ticket", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--adr")
    ap.add_argument("--test-summary")
    ns = ap.parse_args(argv)
    art = build_for_delivery(ns.repo, ns.base, ns.head, ns.ticket, ns.test_summary, ns.adr)
    out = Path(ns.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{ns.ticket}.json").write_text(art.model_dump_json(indent=2), encoding="utf-8")
    (out / f"{ns.ticket}.md").write_text(art.to_markdown(), encoding="utf-8")
    print(f"learning pack {ns.ticket}: {len(art.files)} arquivos, {len(art.rejected_claims)} afirmacoes rejeitadas -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
