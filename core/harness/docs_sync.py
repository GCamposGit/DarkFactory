"""Checagem docs-sync do manual (USR-376).

Cada pagina ``docs/manual/*.md`` declara na primeira linha
``<!-- covers: caminho1, glob/*.py -->``. Se um arquivo coberto muda no
intervalo ``base...head`` e a pagina nao muda, a checagem falha. Um commit
com o trailer ``Docs-Unchanged: <motivo>`` libera o intervalo.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MANUAL_DIR = "docs/manual"
_COVERS_RE = re.compile(r"^\s*<!--\s*covers:\s*(.*?)\s*-->\s*$")
_TRAILER_RE = re.compile(r"^Docs-Unchanged:[ \t]*(\S.*)$", re.MULTILINE)


@dataclass(frozen=True)
class Violation:
    covered_file: str
    page: str

    def __str__(self) -> str:
        return f"{self.covered_file} -> {self.page}"


def parse_covers(first_line: str) -> list[str]:
    """Extrai os padroes do cabecalho ``<!-- covers: ... -->`` (vazio se ausente)."""
    match = _COVERS_RE.match(first_line)
    if not match:
        return []
    return [part.strip().replace("\\", "/") for part in match.group(1).split(",") if part.strip()]


def _matches(path: str, pattern: str) -> bool:
    if path == pattern:
        return True
    # fnmatch: '*' tambem atravessa '/', suficiente para globs de modulo.
    return fnmatch.fnmatchcase(path, pattern)


def has_unchanged_trailer(messages: str) -> bool:
    """True se alguma mensagem de commit tem ``Docs-Unchanged: <motivo>`` nao vazio."""
    return bool(_TRAILER_RE.search(messages))


def find_violations(
    changed: list[str], pages: dict[str, list[str]]
) -> list[Violation]:
    """``pages`` mapeia pagina -> padroes cobertos. Arquivos em ``tests/`` nunca disparam."""
    changed_set = {c.replace("\\", "/") for c in changed}
    out: list[Violation] = []
    for page, patterns in sorted(pages.items()):
        if page in changed_set:
            continue
        for path in sorted(changed_set):
            if path.startswith("tests/") or path.startswith(MANUAL_DIR + "/"):
                continue
            if any(_matches(path, pat) for pat in patterns):
                out.append(Violation(path, page))
    return out


def load_pages(root: Path) -> dict[str, list[str]]:
    pages: dict[str, list[str]] = {}
    manual = root / MANUAL_DIR
    if not manual.is_dir():
        return pages
    for md in sorted(manual.glob("*.md")):
        rel = f"{MANUAL_DIR}/{md.name}"
        with md.open(encoding="utf-8") as fh:
            first = fh.readline()
        covers = parse_covers(first)
        if not covers:
            print(f"aviso: {rel} sem cabecalho 'covers:' (nao cobre nada)", file=sys.stderr)
        pages[rel] = covers
    return pages


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} falhou: {proc.stderr.strip()}")
    return proc.stdout


def check(root: Path, base: str, head: str) -> list[Violation]:
    changed = [
        line for line in _git(root, "diff", "--name-only", f"{base}...{head}").splitlines() if line
    ]
    messages = _git(root, "log", "--format=%B", f"{base}..{head}")
    if has_unchanged_trailer(messages):
        return []
    return find_violations(changed, load_pages(root))


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="Checagem docs-sync do manual")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    try:
        violations = check(Path(args.root).resolve(), args.base, args.head)
    except RuntimeError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 2
    if not violations:
        print("docs-sync: ok")
        return 0
    print("docs-sync: arquivo coberto alterado sem alterar a pagina do manual:")
    for v in violations:
        print(f"  {v.covered_file} -> {v.page}")
    print(
        "Atualize a pagina ou adicione ao commit o trailer 'Docs-Unchanged: <motivo>'."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
