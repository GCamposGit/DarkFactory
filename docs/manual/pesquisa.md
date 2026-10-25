<!-- covers: core/research/policy.py, core/research/ledger.py -->
# Pesquisa condicional

O estagio de pesquisa (`core/research/policy.py`) so roda quando vale a pena.

## Quando pesquisa
- Arquitetura (regra R4), integracao externa (R5), ou feature/infraestrutura/seguranca de complexidade media ou maior (R6).

## Quando dispensa
- Bug, correcao, chore, docs, teste (R1), tipos documentation/quality/operations (R2), mudanca pequena (R3). O motivo fica gravado como artefato `research_skipped`.

## Ledger primeiro
1. `lookup` procura tema parecido em `.factory/research/`. Se achar, o artefato e `research_reused` e nao ha busca nova.
2. Senao, a busca web injetada roda e o ledger recebe: fonte com URL, data de acesso, insight e "Como influenciou o plano".
3. Todo artefato fica em `.factory/research/_artifacts/<ticket>.json`. Fonte sem URL e rejeitada.

Teste: `python -m pytest C:\dev\DarkFac\tests\test_research_policy.py -q`

Ledger antigo com formato que nao abre mais (enums ou campos de versoes anteriores) e ignorado com aviso no log: nunca derruba o estagio RESEARCH.
