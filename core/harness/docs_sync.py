"""Checagem docs-sync do manual (USR-376).

Cada pagina ``docs/manual/*.md`` declara na primeira linha
``<!-- covers: caminho1, glob/*.py -->``. Se um arquivo coberto muda no
intervalo ``base...head`` e a pagina nao muda, a checagem falha. Um commit
com o trailer ``Docs-Unchanged: <motivo>`` libera o intervalo.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MANUAL_DIR = "docs/manual"
_COVERS_RE = re.compile(r"^\s*<!--\s*covers:\s*(.*?)\s*-->\s*$")
_TRAILER_RE = re.compile(r"^Docs-Unchanged:[ \t]*(\S.*)$", re.MULTILINE | re.IGNORECASE)


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


INTERNAL_TRAILER = "Docs-Unchanged: ajuste interno sem mudanca de contrato operacional"
# Superficie publica: parser de CLI, rotas HTTP e contratos que o owner opera. Compara a AST
# canonica antes/depois; na duvida (nao-.py, D/R/C, erro de parse) nao ha trailer.
_OWNER_CONTRACT_PATHS = (".env.example", ".factory/config/", ".factory/owner_actions/")
_SURFACE_CALLS = frozenset({
    "add_argument", "add_parser", "add_subparsers", "add_argument_group", "add_mutually_exclusive_group",
    "set_defaults", "APIRouter", "FastAPI", "include_router", "mount", "add_api_route", "add_api_websocket_route",
})
_ROUTE_DECORATORS = frozenset({"get", "post", "put", "delete", "patch", "api_route", "websocket", "route"})


def _call_name(func: ast.expr) -> str:
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def _surface(source: str) -> list[str]:
    """Multiconjunto ordenado de chamadas de superficie e decorators de rota (``SyntaxError`` sobe)."""
    out: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and _call_name(node.func) in _SURFACE_CALLS:
            out.append(ast.dump(node))
        decorators = getattr(node, "decorator_list", [])
        out += [ast.dump(d) for d in decorators if isinstance(d, ast.Call) and _call_name(d.func) in _ROUTE_DECORATORS]
    return sorted(out)


def _blob(root: Path, spec: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", spec], cwd=root, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=20,
    )
    return proc.stdout if proc.returncode == 0 else None


def internal_only_trailer(root: Path, message: str = "") -> str:
    """Trailer ``Docs-Unchanged`` quando o indice so traz ajuste interno de modulo coberto pelo manual.

    Conservador: vazio se ja ha trailer, se nada coberto muda, se algum arquivo coberto nao e .py
    ou foi apagado/renomeado/copiado, se algum .py coberto muda a superficie (AST) ou nao da para
    comparar, ou se um contrato do owner muda (a documentacao segue exigida).
    """
    if has_unchanged_trailer(message):
        return ""
    status = _git(root, "diff", "--cached", "--name-status").splitlines()
    staged = [path for line in status for path in line.split("\t")[1:]]
    covered = {v.covered_file for v in find_violations(staged, load_pages(root))}
    if not covered or any(p.startswith(_OWNER_CONTRACT_PATHS) for p in staged):
        return ""
    for line in status:
        kind, *paths = line.split("\t")
        if covered.intersection(paths) and (kind[0] != "M" or not paths[0].endswith(".py")):
            return ""
    for path in sorted(covered):
        before, after = _blob(root, f"HEAD:{path}"), _blob(root, f":{path}")
        try:
            if before is None or after is None or _surface(before) != _surface(after):
                return ""
        except SyntaxError:
            return ""
    return INTERNAL_TRAILER


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


DICTIONARY_PAGE = f"{MANUAL_DIR}/dicionario_dados.md"
_PATHS_RE = re.compile(r"^\s*<!--\s*paths:\s*(.*?)\s*-->\s*$")


def check_dictionary(root: Path) -> list[str]:
    """Lista os ``.factory/**/*.json`` versionados sem padrao ``<!-- paths: -->`` no dicionario."""
    page = root / DICTIONARY_PAGE
    patterns: list[str] = []
    if page.is_file():
        for line in page.read_text(encoding="utf-8").splitlines():
            match = _PATHS_RE.match(line)
            if match:
                patterns += [p.strip() for p in match.group(1).split(",") if p.strip()]
    tracked = [
        p for p in _git(root, "-c", "core.quotepath=off", "ls-files", "-z", ".factory").split(chr(0))
        if p.endswith(".json")
    ]
    return sorted(p for p in tracked if not any(_matches(p, pat) for pat in patterns))


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="Checagem docs-sync do manual")
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--dictionary", action="store_true", help="checa .factory/*.json sem entrada no dicionario"
    )
    args = parser.parse_args(argv)
    if args.dictionary:
        try:
            missing = check_dictionary(Path(args.root).resolve())
        except RuntimeError as exc:
            print(f"erro: {exc}", file=sys.stderr)
            return 2
        for path in missing:
            print(f"dicionario: sem entrada para {path}")
        if not missing:
            print("dicionario: ok")
        return 1 if missing else 0
    if not args.base or not args.head:
        parser.error("--base e --head sao obrigatorios sem --dictionary")
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
