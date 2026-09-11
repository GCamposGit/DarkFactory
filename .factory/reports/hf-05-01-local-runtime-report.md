# HF-05-01 — relatório do runtime local

## Resumo

- Ticket: `HF-05-01`
- Parent: `HF-05`
- Escopo: runtime headless local, sem rede, PostgreSQL, DBOS, cloud ou credenciais
- Base: working tree `codex/hf-01-baseline`; `.git` permanece somente leitura
- Canaletto: não alterado
- Status: implementação local validada; commit/push/PR/merge continuam bloqueados

## Entregue

- `core/workflow/runtime.py`
  - contratos Pydantic fechados para run, job, lease, outbox e reconciliação;
  - store SQLite transacional único por runtime;
  - transição + evento outbox na mesma transação;
  - deduplicação de jobs, eventos e conclusões;
  - leases com ownership, expiração e histórico por tentativa;
  - dependências, locks por chave, limite por etapa e fairness entre projetos;
  - reserva/consumo de orçamento, `waiting_budget` e cancelamento liberando recursos;
  - reconciliação de leases expiradas e outbox persistente após restart.
- `core/workflow/readiness.py`
  - cancelamento legal explicitamente permitido nos estados intermediários
    relevantes, sem abrir transições de entrega indevidas.
- `core/workflow/__init__.py`
  - exports públicos dos contratos e runtime.
- `tests/test_workflow_runtime.py`
  - 11 testes determinísticos cobrindo persistência, idempotência, dependências,
    capacidade, locks, fairness, ownership, retry, orçamento, cancelamento e
    restart do outbox.
- `docs/handoffs/HF-05.md`
  - handoff específico do slice local e sucessor condicionado a HF-03/acesso real.
- `.factory/context_intelligence.json`
  - contexto atualizado com arquitetura, invariantes e validação do HF-05-01.
- `.factory/learning_packs/pack_20260909_175239_fd1f75.*`
  - Learning Pack gerado para o marco arquitetural, com Markdown, HTML, JSON e Anki TSV.

## Validação

| Comando | Resultado |
| --- | --- |
| `python -m pytest tests/test_workflow_runtime.py tests/test_workflow_contracts.py -v` | 24 passed |
| `python core/orchestrator/guard.py HEAD` | `[GUARD PASS]` |
| `python core/harness/runner.py --quick` | `[HARNESS_PASS]`, 501 collected, 499 passed, 2 skipped |
| `python -m pytest tests -v --ignore=tests/test_canaletto.py` | 501 collected, 499 passed, 2 skipped |
| `python -m json.tool .factory/context_intelligence.json` | `[CONTEXT_JSON_PASS]` |

Os dois skips são os mesmos probes existentes: áudio live desabilitado e
criação de symlink indisponível neste host Windows.

## RCA aplicado

1. A tabela de leases inicialmente impedia histórico de tentativas por uma
   restrição `UNIQUE(job_id)`. Ela foi substituída por índice único somente para
   leases ativas, preservando histórico e fencing.
2. O teste de conflito dependia de uma ordem incidental com timestamps iguais;
   passou a declarar prioridade explícita.
3. O teste de restart confundia ack de um evento com drenagem total da fila;
   passou a ackar cada evento e repetir um ack para provar idempotência.
4. A revisão preventiva mostrou que retry agendado podia sobreviver ao
   cancelamento do run. O status foi incluído no cancelamento e recebeu teste de
   regressão; cancelamento também foi aberto somente nos estados de ciclo
   pertinentes.

## Auditoria e limites

O guardrail de caminhos protegidos passou. A tentativa de auditoria local com
`gpt-review:latest` terminou sem veredito porque o modelo não tinha acesso ao
filesystem; portanto não foi contabilizada como aprovação independente. A
validação determinística e a revisão de escopo foram executadas localmente.

Este relatório não declara `operationally_verified` ou `delivered`: o slice é
uma implementação local/simulada. A próxima unidade, `HF-05-02`, depende do
handoff de HF-03 e de acesso real ao worker/controle cloud. Nenhum commit, push,
PR, merge ou verificação de SHA remoto foi possível porque `.git` é somente
leitura.
