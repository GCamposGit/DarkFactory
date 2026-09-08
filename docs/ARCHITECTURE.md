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

---

## 3. Vertical E2E de Referência: Echo Garden

`core/game/` isola o domínio determinístico do jogo das integrações de modelo, imagem e apresentação. O motor aceita estado + movimento e retorna um novo estado sem rede, relógio ou I/O. A camada one-shot recebe contribuições estruturadas dos três tiers, valida-as contra o motor e somente então exporta o HTML autocontido.

O driver oficial é `cli`, com chamadas `library` nos níveis unitário e de integração. A suíte live é separada da validação compartilhada para não consumir APIs em clones limpos, mas não aceita `skip` como sucesso durante um ensaio solicitado: credencial ausente, modelo indisponível, resposta inválida ou fallback de provedor falham o gate.

Os limites de segurança são:

- respostas de LLM são dados validados, nunca código executado;
- chaves permanecem no ambiente/registro e não entram nos artefatos;
- chamadas cloud têm timeout e erro estruturado;
- imagens usam bytes base64 retornados pela API oficial e limites de tamanho;
- o HTML não carrega bibliotecas, scripts ou conteúdo remoto.

O plano completo e os critérios de aceitação estão em `docs/DARK_FACTORY_E2E_TEST_PLAN.md`.
