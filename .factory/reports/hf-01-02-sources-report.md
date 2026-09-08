# HF-01-02 — contratos e leitura segura

## Identidade

- **Ticket:** `HF-01-02`
- **Origem:** `user-demand`, derivado do roadmap integrado de 08/09/2026
- **Planejador:** Astra, conforme `docs/handoffs/HF-01.md`
- **Owner da execução:** Codex
- **Branch:** `codex/usr-16-onprem-worker`
- **Worktree:** `C:\dev\DarkFac`
- **SHA-base observado:** `3ab81930e38a4832069a143c97cf9599a8bf477c`
- **SHA final da implementação:** `9458e21`
- **Dependência:** HF-01-01 entregue no SHA-base

## Entrega

- `core/planning/baseline_models.py` define contratos Pydantic v2 estritos para catálogo, declarações, observações, claims, issues, assessments e snapshot; timestamps precisam ser timezone-aware e os estados de evidência permanecem separados.
- `core/planning/baseline_sources.py` implementa `load_catalog` e `collect_sources` sem executar conteúdo, sem retornar payload bruto e sem escrever nas fontes.
- Paths são relativos, sem glob, contidos na raiz resolvida e rejeitam escape por `..` ou symlink externo. Cada leitura é limitada a 2 MiB e exige UTF-8 estrito.
- JSON de roadmap e tabelas Markdown com cabeçalho declarado produzem `PlannedItem`; inventários, relatórios e arquivos de código apenas registram presença/hash.
- Hashes são comparados antes/depois do parsing; uma divergência provoca no máximo uma releitura e termina em `SOURCE_UNSTABLE`.
- `tests/test_baseline_sources.py` cobre extração, esquema/corrupção/encoding, acesso negado, path escape, symlink quando o host permite, limite de tamanho, instabilidade, ausência de promoção `verified` e contratos.

Na coleta real do catálogo, 54 fontes foram observadas e 49 declarações foram extraídas. Duas entradas Markdown ficaram explicitamente em `SOURCE_SCHEMA_CHANGED` porque o cabeçalho catalogado não coincide exatamente com a fonte atual (`development-plan` e `infra-roadmap-phase-4`); o coletor não adivinha cabeçalhos alternativos. Isso permanece como pendência de reconciliação do HF-01-01/HF-01-03, não como promoção silenciosa.

## Validação

| Comando | Resultado | Exit code |
| --- | --- | ---: |
| `python core/harness/terminal_env.py --check` | `[TERMINAL_ENV_PASS]` | 0 |
| `ruff check core/planning tests/test_baseline_sources.py` | todos os checks passaram | 0 |
| `python -m pytest tests/test_baseline_sources.py tests/test_baseline_catalog.py -v` | 9 passed, 1 skipped; 10 coletados | 0 |
| `python core/harness/runner.py --quick` | `[HARNESS_PASS]`; 423 passed, 2 skipped; 425 coletados | 0 |
| `python -m pytest tests -v --ignore=tests/test_canaletto.py` | 423 passed, 2 skipped; 425 coletados | 0 |
| `git diff --cached --check` | sem erros antes do commit | 0 |

O skip do teste de symlink ocorreu porque a política do host Windows não permitiu criar symlink sem privilégio; o caminho `..` foi exercitado e a resolução do coletor mantém o bloqueio de symlink externo.

## Estado residual e entrega

- **Heartbeat final:** execução local concluída; nenhuma frente adicional foi despachada e nenhum arquivo de Canaletto foi alterado.
- Alterações concorrentes do USR-18 foram preservadas fora do commit seletivo, incluindo `.factory/`, `core/infra/`, `hub/`, `deploy/` e `tests/test_darkhub_cloud_gateway.py`.
- O commit local `9458e21` contém somente o incremento HF-01-02, seu teste e o ledger de aprendizagem desta sessão.
- Push, PR, checks remotos e merge não foram executados neste checkout compartilhado; portanto a integração remota exigida por `FACTORY_RULES.md` permanece pendente.
