"""Synthetic, read-only Jev routing probes for the Dark Factory."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


OUTPUT_PATH = Path(__file__).with_name("results.json")
ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.13"

CASES = [
    {
        "id": "localized_windows_fix",
        "expected_stage": "coding",
        "expected_complexity": "low",
        "text": "Implementar correção localizada em um CLI Python 3.12: saída UTF-8 falha apenas no console Windows. Há contrato e teste de regressão definidos; um arquivo de produção e um teste mudam. Após implementar, rodar o harness determinístico.",
    },
    {
        "id": "budget_architecture",
        "expected_stage": "planning",
        "expected_complexity": "high",
        "text": "Planejar mudança no contrato de reserva de orçamento para jobs concorrentes em três projetos, com idempotência após crash e reconciliação. Definir interfaces, invariantes e migração antes de escrever código. O planejador precisa ser modelo de alta inteligência qualificado.",
    },
    {
        "id": "independent_review",
        "expected_stage": "review",
        "expected_complexity": "medium",
        "text": "Fazer revisão adversarial independente do patch já implementado por DeepSeek, buscando contraexemplos reproduzíveis de dupla reserva. Não alterar o código. O revisor precisa ser de outra família de modelos.",
    },
    {
        "id": "deterministic_tests",
        "expected_stage": "testing",
        "expected_complexity": "low",
        "text": "Executar python core/harness/runner.py --quick e pytest no checkout pronto, coletar marcadores e códigos de saída e registrar falhas. Não gerar código novo nem usar modelo como oráculo dos testes.",
    },
    {
        "id": "confidential_offline_fix",
        "expected_stage": "coding",
        "expected_complexity": "medium",
        "text": "Corrigir serialização Pydantic em módulo com dados confidenciais de cliente. O conteúdo não pode sair da máquina; usar apenas executor local/offline. Contrato aprovado, dois arquivos afetados, validação focal definida.",
    },
    {
        "id": "live_docs_research",
        "expected_stage": "research",
        "expected_complexity": "medium",
        "text": "Pesquisar na documentação oficial atual de uma biblioteca Python recém-atualizada quais mudanças da API afetam o adaptador HTTP, registrar links e diferenças. Sem implementação neste ticket.",
    },
    {
        "id": "ambiguous_payment_feature",
        "expected_stage": "planning",
        "expected_complexity": "high",
        "text": "Adicionar pagamentos recorrentes e reembolsos ao produto. O owner ainda não definiu provedor, países, moedas, prazos de reembolso ou quem pode aprovar estornos. Preparar Grill antes de especificar ou codificar.",
    },
    {
        "id": "critical_auth_incident",
        "expected_stage": "planning",
        "expected_complexity": "critical",
        "text": "Produção apresentou aparente falha de isolamento entre tenants após alteração de autenticação. Ainda não se conhece o alcance. Planejar contenção, diagnóstico e critérios de verificação com modelo de alta inteligência antes de mudanças irreversíveis.",
    },
]

QUESTIONS = {
    "stage": {
        "type": "choice",
        "instructions": "Qual é a etapa principal solicitada agora pela tarefa? Julgue a ação imediata, não etapas futuras mencionadas no texto.",
        "criteria": {
            "planning": "Esclarecer requisitos, desenhar solução, arquitetura ou plano antes de implementação.",
            "coding": "Implementar ou corrigir código conforme contrato já definido.",
            "review": "Revisar independentemente um patch ou solução já produzida.",
            "testing": "Executar verificadores determinísticos ou coletar resultados de teste.",
            "research": "Pesquisar fontes externas ou documentação e sintetizar achados sem implementar.",
            "unknown": "Nenhuma etapa acima está suficientemente especificada.",
        },
    },
    "complexity": {
        "type": "choice",
        "instructions": "Qual a complexidade técnica e o risco da etapa principal, considerando escopo e impacto descritos?",
        "criteria": {
            "low": "Mudança ou execução localizada, contrato claro, impacto pequeno e verificadores disponíveis.",
            "medium": "Mais de um componente ou casos de borda relevantes, mas interfaces e impacto controláveis.",
            "high": "Contratos transversais, concorrência, arquitetura ou impacto operacional elevado.",
            "critical": "Incidente de segurança, isolamento de dados ou produção com alcance desconhecido e alto impacto.",
        },
    },
    "needs_owner_decision": {
        "type": "noul",
        "instructions": "Há alguma decisão material de produto, permissão ou regra de negócio ainda não especificada que somente o owner pode fornecer antes de especificar ou implementar? Não conte escolhas técnicas resolvíveis pelo planejador.",
    },
    "must_stay_local": {
        "type": "noul",
        "instructions": "O texto exige explicitamente que dados ou execução permaneçam locais/offline, sem enviar conteúdo a um provedor de nuvem?",
    },
}


def main() -> None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY unavailable")

    records = []
    for case in CASES:
        payload = {
            "model": MODEL,
            "state": {"task": case["text"], "environment": "Dark Factory Python; preserve human Grill, qualified roles, budget and deterministic validation."},
            "questions": QUESTIONS,
        }
        request = urllib.request.Request(
            ENDPOINT,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.load(response)
            record = {
                "id": case["id"],
                "task": case["text"],
                "expected_stage": case["expected_stage"],
                "expected_complexity": case["expected_complexity"],
                "model": result.get("model"),
                "provider": result.get("provider"),
                "answers": result.get("answers"),
                "usage": result.get("usage"),
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        except urllib.error.HTTPError as exc:
            record = {"id": case["id"], "error_status": exc.code, "error_body": exc.read(1000).decode("utf-8", "replace")}
        records.append(record)
        print(json.dumps({
            "id": record["id"],
            "stage": (record.get("answers") or {}).get("stage", {}).get("choice"),
            "complexity": (record.get("answers") or {}).get("complexity", {}).get("choice"),
            "owner_p": (record.get("answers") or {}).get("needs_owner_decision", {}).get("noul"),
            "local_p": (record.get("answers") or {}).get("must_stay_local", {}).get("noul"),
            "latency_ms": record.get("latency_ms"),
            "error_status": record.get("error_status"),
        }, ensure_ascii=False), flush=True)

    output = {"created_at": datetime.now(UTC).isoformat(), "model_requested": MODEL, "endpoint": ENDPOINT, "synthetic_cases": True, "records": records}
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
