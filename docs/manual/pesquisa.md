<!-- covers: core/research/policy.py, core/research/ledger.py, core/research/compat.py, core/research/cli.py -->
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

## Compatibilidade e migracao de ledgers antigos

O leitor aceita variantes historicas conhecidas e normaliza durante a leitura: `primary`, `official`, `canonical` e `documentation` viram `high_credibility`; `secondary`, `community`, `trend` e `social` viram `trend_signal`. Tambem converte `canonical_url` para `url`, `question` para `query`, `consulted_on` para `created_at` e `consulted_at`/`accessed_on` para `metadata.accessed_at`. Campos antigos sem equivalente ficam preservados em `provenance` do ledger ou em `metadata` da fonte. A migracao regrava as fontes normalizadas e regenera `INSIGHTS.md`.

Use primeiro o modo de simulacao, que nao escreve arquivos. No PowerShell, a partir de qualquer diretório:

```powershell
Set-Location C:\dev\DarkFac
python -m core.research.cli migrate-ledgers
```

O resultado JSON lista apenas ledgers historicos, os caminhos de campos que seriam normalizados e contagens de fontes/insights. Para aplicar a todos, informe explicitamente `--apply`; `--ledger-id` limita a operacao a um ledger:

```powershell
Set-Location C:\dev\DarkFac
python -m core.research.cli migrate-ledgers --apply
python -m core.research.cli migrate-ledgers --apply --ledger-id 20260908_hybrid_workflow
```

Cada registro que nao puder ser normalizado aparece em `errors` com `ledger_id`, `field` e `reason`, sem incluir valores de fonte ou credenciais. O CLI continua migrando os demais e retorna codigo diferente de zero se houver erro. Ledger historico com campos ou enums desconhecidos continua disponivel para diagnostico, mas nao e regravado automaticamente.
