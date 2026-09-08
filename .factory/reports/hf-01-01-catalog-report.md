# HF-01-01 — catálogo e evidências curadas

## Identidade

- **Ticket:** `HF-01-01`
- **Origem:** `user-demand`, derivado do roadmap integrado de 08/09/2026
- **Planejador:** Astra, conforme handoff aprovado em `docs/handoffs/HF-01.md`
- **Owner da execução:** Codex
- **Branch:** `codex/usr-16-onprem-worker`
- **Worktree:** `C:\dev\DarkFac`
- **SHA-base observado:** `0a1bfe32949f6de403191f390c2867bba97ed76b`
- **Plano:** `docs/handoffs/HF-01.md`, versão 1.0

## Entrega

O primeiro recorte do HF-01 foi entregue sem código de domínio, rede, mutações do Hub ou alteração do experimento Canaletto:

- `docs/handoffs/hf01-sources.json` cataloga fontes finitas e resolvíveis, com cabeçalhos e IDs explícitos para tabelas Markdown.
- `docs/handoffs/hf01-claims.json` separa claims de implementação, integração e operação, preservando limitações, locators e SHA-256.
- `tests/fixtures/baseline_cases.json` congela os resultados esperados dos casos A1–A10, incluindo conflitos, evidência stale, 404, acesso ausente e coleta instável.
- `tests/test_baseline_catalog.py` verifica JSON, contenção de paths, unicidade, hashes, ausência de segredos/commands e fidelidade dos casos.

O catálogo inclui explicitamente `usr-16-report.md` e não inclui fontes do Canaletto. A declaração do owner sobre PostgreSQL, o link genérico do n8n e a divergência de status entre fontes permanecem claims distintos; nenhuma conclusão operacional foi promovida silenciosamente.

## Validação

| Comando | Resultado | Exit code |
| --- | --- | ---: |
| `python core/harness/terminal_env.py --check` | `[TERMINAL_ENV_PASS]` | 0 |
| `python -m pytest --collect-only -q tests --basetemp C:\dev\DarkFac\.factory\tmp-hf01-01-preflight` | 419 coletados no preflight, antes do teste focal existir | 0 |
| `python -m pytest tests/test_baseline_catalog.py -v` | 3 passed | 0 |
| `python core/harness/runner.py --quick` | `[HARNESS_PASS]`; 417 passed, 1 skipped; 418 coletados | 0 |
| `python -m pytest tests -v --ignore=tests/test_canaletto.py` | 417 passed, 1 skipped; 418 coletados | 0 |
| `git diff --check` | sem erros nos arquivos da unidade | 0 |

O primeiro harness após a escrita encontrou uma divergência de hash porque outra frente alterou `.factory/infra/roadmap.md` enquanto a unidade executava. A causa foi registrada; as claims de status foram ancoradas no `infra-roadmap.json`, fonte estável da baseline, e o harness foi repetido com sucesso.

## Escopo residual e entrega

Alterações pré-existentes ou concorrentes foram preservadas fora do commit desta unidade, incluindo os arquivos do USR-18 em `.factory/`, `core/infra/`, `hub/`, `deploy/` e `tests/`. O SHA final do commit seletivo e a verificação remota são emitidos no handoff para evitar auto-referência do próprio relatório.

HF-01-02 permanece como próximo ticket; este recorte não implementa contratos, leitura segura, reconciliação, probes ou CLI.
