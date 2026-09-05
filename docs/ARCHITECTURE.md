# Arquitetura da Fábrica de Software Autônoma (Dark Factory)

A **Dark Factory Multi-Modelo** é uma infraestrutura de engenharia autônoma projetada para receber especificações e entregar software validado, testado e mesclado sem intervenção humana no teclado (Nível 3 de autonomia).

---

## 1. Topologia do Sistema

A fábrica é composta por três camadas integradas:

```text
┌─────────────────────────────────────────────────────────────┐
│                    1. Camada de Intenção                    │
│   • PRD / Issues do GitHub / Especificação de Requisitos    │
│   • Lista Inegociável de Non-Goals (MISSION.md)            │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│             2. Roteador Híbrido (Local + Nuvem)             │
│   • Ollama Local: qwen-fast (Micro-tarefas), qwen-deep      │
│   • Antigravity Core: Gemini 3.8 Flash (Orquestrador)       │
│   • Nuvem Especializada: Grok 4.6, Claude 3.7, DeepSeek-R1  │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│            3. Motor de Execução & Validação (PIV)           │
│   • Prime -> Plan -> Implement -> Validate                  │
│   • Portões Determinísticos (runner.py + markers.py)        │
│   • Auditoria Adversarial Cruzada (gpt-review + Nuvem)      │
│   • Guardrail de Arquivos Protegidos (guard.py)             │
│   • Auto-Merge em Nível 3 (Green-Gate Only)                 │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. Os 5 Componentes Estruturais

1. **Guidance Layer**:
   - `MISSION.md`: Define a visão, a jornada principal e os *non-goals* permanentes.
   - `FACTORY_RULES.md`: Regras de execução sem supervisão e limites operacionais.
   - `AGENTS.md`: Convenções de código, tipagem e padrões de engenharia.
2. **Validation Harness**:
   - Escada determinística em 5 níveis: Análise Estática -> Testes Unitários -> Integração -> E2E Headless -> Testes Holdout.
   - Contrato de marcadores: `[STEP_START]`, `[STEP_PASS]`, `[STEP_FAIL]`, `[TEST_COUNT]`, `[HARNESS_PASS]`.
3. **Workflow Engine**:
   - Máquina de estados determinística (`core/orchestrator/state.py`).
   - Guardrail de caminhos protegidos (`core/orchestrator/guard.py`).
4. **Deploy & Entrega Contínua**:
   - Pipeline automatizado de empacotamento e entrega acionado pós-merge.
5. **Trigger / Agendador Autônomo**:
   - Agendamento de polling via Task Scheduler (Windows) ou Cron/Systemd (Linux) que dispara o ciclo de trabalho sem depender de webhooks frágeis.
