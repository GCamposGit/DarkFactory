"""Run and persist a critical review of any DarkFac skill through OpenRouter."""

from __future__ import annotations

import json
import os
import sys
import time
import argparse
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"


def api_key() -> str | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key and sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as handle:
                key, _ = winreg.QueryValueEx(handle, "OPENROUTER_API_KEY")
        except (OSError, ImportError):
            pass
    return key.strip() if key else None


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skill", help="Caminho relativo para o SKILL.md a analisar")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Modelo OpenRouter")
    parser.add_argument("--output", type=Path, help="Arquivo de saída relativo ao repositório")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    skill_path = (ROOT / args.skill).resolve()
    if not skill_path.is_file() or skill_path.name.upper() != "SKILL.MD":
        print("[ERRO] Informe um caminho existente terminando em SKILL.md.")
        return 1
    output = (ROOT / args.output).resolve() if args.output else ROOT / ".factory" / "reviews" / f"{skill_path.parent.name}_{args.model.replace('/', '_').replace(':', '_')}_review.md"
    key = api_key()
    if not key:
        print("[ERRO] OPENROUTER_API_KEY nao encontrada no ambiente ou registro.")
        return 1

    context_files = [
        "AGENTS.md",
        "MISSION.md",
        "FACTORY_RULES.md",
        str(skill_path.relative_to(ROOT)),
        ".claude/skills/" + skill_path.parent.name + "/SKILL.md" if str(skill_path).startswith(str(ROOT / ".agents")) else ".agents/skills/" + skill_path.parent.name + "/SKILL.md",
    ]
    context_files = [path for path in context_files if (ROOT / path).exists()]
    keyword = skill_path.parent.name.split("-", 1)[-1].split("_")[0].lower()
    related = [
        path
        for base in (ROOT / "core", ROOT / "tests")
        for path in base.rglob("*")
        if path.is_file() and keyword in path.name.lower()
    ]
    context_files.extend(str(p.relative_to(ROOT)) for p in related[:12])
    context = "\n\n".join(
        f"===== {path} =====\n{read(ROOT / path)}" for path in context_files
    )
    skill_name = skill_path.parent.name
    prompt = f"""Analise criticamente a skill `{skill_name}` da DarkFac.

Você é um revisor adversarial independente, Staff+ e orientado a contratos. O objetivo é
encontrar falhas reais de especificação, governança, segurança, determinismo, observabilidade,
idempotência e eficácia — não apenas elogiar ou reescrever superficialmente.

Contexto operacional: DarkFac é uma fábrica headless Python 3.12+, Windows/Linux, com
`.agents/skills/` como fonte canônica e `.claude/skills/` como espelho. A Skill 00 governa
melhoria contínua de skills a partir de evidências, RCA, patches delimitados e validação.

Entregue em português, com esta estrutura:
1. Veredito executivo e nível de risco.
2. Pontos fortes comprováveis.
3. Achados priorizados (P0/P1/P2), cada um com evidência no contexto, mecanismo de falha,
   impacto, contraexemplo mínimo reproduzível e correção recomendada.
4. Auditoria dos contratos Inputs, Ações, Outputs e Portões.
5. Casos de abuso/adversariais e condições de corrida/replay.
6. Lacunas entre a especificação e a implementação/testes observados.
7. Plano de melhoria mínimo, ordenado, sem enfraquecer gates.
8. Veredito final: `changes_required`, `no_actionable_findings_in_reviewed_scope` ou `incomplete`.

Não invente fatos ausentes: marque explicitamente limites de evidência. Não exponha segredos.

MATERIAL DE ANÁLISE:
{context}
"""
    payload = {
        "model": args.model,
        "messages": [
            {"role": "system", "content": "Você realiza auditorias técnicas adversariais rigorosas de contratos de agentes."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 6000,
        "temperature": 0.2,
        "reasoning": {"exclude": True},
    }
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/DarkFac",
            "X-Title": f"DarkFac {skill_name} Critical Review",
        },
        data=json.dumps(payload).encode("utf-8"),
    )
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"[FALHA HTTP {exc.code}] {exc.read().decode('utf-8', errors='replace')}")
        return 1
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[FALHA] {exc}")
        return 1

    message = data.get("choices", [{}])[0].get("message", {})
    content = message.get("content", "")
    if isinstance(content, list):
        content = "\n".join(
            item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"
        )
    usage = data.get("usage", {})
    if not content:
        print("[FALHA] OpenRouter retornou resposta sem texto.")
        print(json.dumps({"keys": sorted(data), "message_keys": sorted(message), "error": data.get("error"), "model": data.get("model"), "choices_count": len(data.get("choices", []))}, ensure_ascii=False))
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "skill": skill_name,
        "model_requested": args.model,
        "model_returned": data.get("model"),
        "elapsed_seconds": round(time.time() - started, 2),
        "usage": usage,
        "context_files": context_files,
    }
    output.write_text(
        f"# Revisão crítica — {skill_name}\n\n"
        "## Metadados da execução\n\n```json\n"
        + json.dumps(metadata, ensure_ascii=False, indent=2)
        + "\n```\n\n## Resposta integral do modelo\n\n"
        + content
        + "\n",
        encoding="utf-8",
    )
    print(f"[SUCESSO] Relatorio integral salvo em: {output}")
    print(f"Modelo: {data.get('model')} | Tokens: {usage.get('total_tokens', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
