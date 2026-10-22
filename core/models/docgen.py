"""Generate documentation sections from the model registry (USR-185, slice 4).

Sections live between ``<!-- BEGIN model-registry:<name> -->`` and ``<!-- END model-registry:<name> -->`` markers.
Only the text between the markers is rewritten; everything else in the file is left untouched.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from core.models.registry import ModelRegistry, get_registry

REPO_ROOT = Path(__file__).resolve().parents[2]

# Docs (non-protected) that carry markers: relative path -> sections expected in it.
TARGETS: dict[str, tuple[str, ...]] = {
    "docs/MODEL_SELECTION_GUIDE.md": ("roles", "catalog", "grill_thresholds"),
    ".agents/skills/03-model-router/SKILL.md": ("roles_skill",),
    ".claude/skills/03-model-router/SKILL.md": ("roles_skill",),
    ".agents/skills/17-specialized-test-subagent/SKILL.md": ("test_subagent_model",),
    ".claude/skills/17-specialized-test-subagent/SKILL.md": ("test_subagent_model",),
    ".agents/skills/19-run-ticket/SKILL.md": ("dev_subagent_models", "haiku_rules"),
    ".claude/skills/19-run-ticket/SKILL.md": ("dev_subagent_models", "haiku_rules"),
    ".claude/agents/test-runner.md": ("test_subagent_model",),
}

SUBAGENT_ROUTING_PATH = ".factory/config/subagent_model_routing.json"

_MARKER_RE = re.compile(
    r"(?P<begin><!-- BEGIN model-registry:(?P<name>[a-z0-9_-]+) -->)\r?\n(?P<body>.*?)(?P<end><!-- END model-registry:(?P=name) -->)",
    re.DOTALL,
)


def _cell(value: object) -> str:
    if value is None or value == "" or value == []:
        return "-"
    if isinstance(value, list):
        return ", ".join(f"`{v}`" for v in value)
    return str(value)


def render_roles(reg: ModelRegistry, skip_gateway: bool = False) -> str:
    lines = ["| Papel | Modelo (id) | Status |", "| --- | --- | --- |"]
    by_id = {m.id: m for m in reg.models}
    omitted = 0
    for role in sorted(reg.roles):
        model_id = reg.roles[role]
        if skip_gateway and "/" in model_id:  # gateway ids look like file paths to the skills drift checker
            omitted += 1
            continue
        lines.append(f"| `{role}` | `{model_id}` | {by_id[model_id].status} |")
    if reg.review_pairs:
        lines.append("")
        lines.append("Pares implementador -> revisor (familias distintas): " + "; ".join(
            f"`{p.implementer}` -> `{p.reviewer}`" for p in reg.review_pairs
        ))
    if omitted:
        lines.append("")
        lines.append(f"{omitted} papel(is) de gateway omitido(s); lista completa em `docs/MODEL_SELECTION_GUIDE.md`.")
    lines.append("")
    lines.append(f"Piso de cota: {reg.quota_floor_percent:g}%. Marcadores proibidos em papel autonomo: "
                 + ", ".join(f"`{m}`" for m in reg.forbidden_autonomous_markers) + ".")
    return "\n".join(lines)


def render_catalog(reg: ModelRegistry) -> str:
    lines = [
        "| Modelo (id) | Provedor | Familia | Faixa | Aliases | Status | Entrada US$/Mtok | Saida US$/Mtok | Contexto |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for m in sorted(reg.models, key=lambda e: (e.provider, e.id)):
        flag = " (proibido autonomo)" if m.forbidden_autonomous else ""
        lines.append(
            f"| `{m.id}` | {m.provider} | {m.family} | {m.tier} | {_cell(m.aliases)} | {m.status}{flag} | "
            f"{_cell(m.price_input_per_mtok)} | {_cell(m.price_output_per_mtok)} | {_cell(m.context_tokens)} |"
        )
    return "\n".join(lines)


def render_grill_thresholds(reg: ModelRegistry) -> str:
    """Render the registry-controlled human-decision thresholds."""
    th = reg.grill_thresholds
    lines = [
        "| Limite | Valor |",
        "| --- | --- |",
        f"| Preco maximo de entrada (US$/Mtok) | `{th.max_price_input_per_mtok:g}` |",
        f"| Preco maximo de saida (US$/Mtok) | `{th.max_price_output_per_mtok:g}` |",
        f"| Faixas frontier | {_cell(th.frontier_tiers)} |",
        f"| Grill para preco desconhecido fora de active | `{str(th.require_grill_on_unknown_price).lower()}` |",
        f"| Etapas minimas de qualificacao | `{th.min_qualification_steps}` |",
        "",
        "`promote()` recusa a promocao com `WAITING_HUMAN` quando `requires_grill()` retorna "
        "motivos para a mudanca proposta.",
    ]
    return "\n".join(lines)


def render_roles_skill(reg: ModelRegistry) -> str:
    return render_roles(reg, skip_gateway=True)


def _alias(reg: ModelRegistry, role: str) -> str:
    model_id = reg.roles[role]
    entry = next(m for m in reg.models if m.id == model_id)
    return entry.aliases[0] if entry.aliases else model_id


def render_test_subagent_model(reg: ModelRegistry) -> str:
    return (f"Modelo do subagente de teste no Claude Code: `{reg.roles['subagent.haiku']}` "
            f"(alias `{_alias(reg, 'subagent.haiku')}`, papel `subagent.haiku`), sem *extended thinking*.")


def render_dev_subagent_models(reg: ModelRegistry) -> str:
    return "\n".join([
        f"- **Padrao**: subagente {_alias(reg, 'subagent.sonnet').capitalize()} (`{reg.roles['subagent.sonnet']}`) para todo codigo; "
        f"planning usa {_alias(reg, 'subagent.opus').capitalize()} (`{reg.roles['subagent.opus']}`).",
        f"- **Haiku** (`{reg.roles['subagent.haiku']}`): elegivel apenas pelas regras abaixo.",
    ])


def render_haiku_rules(reg: ModelRegistry) -> str:
    cfg = json.loads((REPO_ROOT / SUBAGENT_ROUTING_PATH).read_text(encoding="utf-8"))
    lim = cfg["limits"]
    types = "; ".join(f"`{t}` ({'/'.join(c)})" for t, c in cfg["eligible_task_types"].items())
    return "\n".join([
        f"- Tipos elegiveis (complexidade): {types}.",
        f"- Limites: ate {lim['max_files']} arquivos e {lim['max_changed_lines']} linhas; {lim['max_haiku_attempts']} tentativa(s) Haiku, "
        f"depois Sonnet (`{reg.roles['subagent.sonnet']}`).",
        "- Tags proibidas: " + ", ".join(f"`{t}`" for t in cfg["forbidden_tags"]) + ".",
        "- Estagios proibidos: " + ", ".join(f"`{t}`" for t in cfg["forbidden_stages"]) + ".",
    ])


RENDERERS: dict[str, Callable[[ModelRegistry], str]] = {
    "roles": render_roles,
    "roles_skill": render_roles_skill,
    "catalog": render_catalog,
    "grill_thresholds": render_grill_thresholds,
    "test_subagent_model": render_test_subagent_model,
    "dev_subagent_models": render_dev_subagent_models,
    "haiku_rules": render_haiku_rules,
}


@dataclass(frozen=True)
class DocResult:
    path: str
    changed: bool
    missing_sections: tuple[str, ...]
    unknown_sections: tuple[str, ...]


def render_text(text: str, reg: ModelRegistry, expected: Optional[tuple[str, ...]] = None) -> tuple[str, DocResult]:
    seen: list[str] = []
    unknown: list[str] = []

    def repl(match: re.Match[str]) -> str:
        name = match.group("name")
        renderer = RENDERERS.get(name)
        if renderer is None:
            unknown.append(name)
            return match.group(0)
        seen.append(name)
        return f"{match.group('begin')}\n{renderer(reg)}\n{match.group('end')}"

    new = _MARKER_RE.sub(repl, text)
    missing = tuple(s for s in (expected or ()) if s not in seen)
    return new, DocResult("", new != text, missing, tuple(unknown))


def process(root: Path = REPO_ROOT, reg: Optional[ModelRegistry] = None, targets: Optional[dict[str, tuple[str, ...]]] = None,
            write: bool = False) -> list[DocResult]:
    reg = reg or get_registry()
    results: list[DocResult] = []
    for rel, expected in (targets or TARGETS).items():
        path = root / rel
        if not path.is_file():
            results.append(DocResult(rel, False, expected, ()))
            continue
        text = path.read_bytes().decode("utf-8")
        new, res = render_text(text, reg, expected)
        if write and res.changed:
            path.write_bytes(new.encode("utf-8"))
        results.append(DocResult(rel, res.changed, res.missing_sections, res.unknown_sections))
    return results


def has_problems(results: list[DocResult]) -> bool:
    return any(r.changed or r.missing_sections or r.unknown_sections for r in results)
