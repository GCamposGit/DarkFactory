# Relatório de Consolidação: USR-70 - Estabilidade do Portão Oficial e Resolução da Onda 2

- **Ticket**: `USR-70`
- **Título**: Portão oficial: falhas intermitentes e fallback local não confiável (Guarda-chuva da Onda 2)
- **Origem**: `user-demand` / `agent`
- **Status do Ticket**: `completed`
- **Data**: 2026-10-03
- **Workspace**: `C:\dev\DarkFac`

---

## 1. Contexto e Causa Raiz Histórica

Em 2026-09-30, o portão oficial apresentou flutuações de contagem de testes (2.164 vs 2.166) e falhas intermitentes durante fallback para execução local no notebook quando o Desktop worker estava offline ou ocupado, acusando 8 falhas em áreas aparentemente não correlacionadas.

A investigação diagnóstica consolidada no plano `docs/STABILITY_PLAN_2026-09-30.md` mapeou as três causas raiz independentes:
1. **Poluição da árvore de trabalho (USR-84)**: Suíte de testes sujava caminhos do workspace durante a execução no Linux/CI, reprovando o portão a posteriori por worktree dirty.
2. **Pressão de memória e COM/WMI RPC nos workers xdist locais (USR-95)**: No Windows do notebook, múltiplos workers xdist concorrentes esgotavam a memória livre disponível gerando falhas `0x8007000e (E_OUTOFMEMORY)` em `platform._wmi_query` e `subprocess._execute_child`.
3. **Acoplamento a estado real do ambiente e concorrência SQLite (USR-88, USR-83)**: Testes escrevendo diretamente em `.factory/notifications`, `.factory/telegram`, e concorrência sobre locks do SQLite nativo sob o driver de runtime spike (`R12 STORE_UNAVAILABLE`).

---

## 2. Consolidação dos Tickets de Sustentação da Onda 2

A resolução do guarda-chuva USR-70 foi integralmente alcançada através da entrega e validação de seus tickets constituintes:

| Ticket | Título | PR / Commit | Status | Solução Entregue |
|---|---|---|---|---|
| **USR-84** | CI do main vermelho: a suite suja a árvore | Commit `e34c9c1` | `completed` | Guarda de higiene e limpeza estrita de arquivos temporários de teste. |
| **USR-88** | Isolar estado mutável da fábrica via `state_root` injetável | PR #128 (`cf658e5`) | `completed` | Injeção de `state_root` com isolamento em `tests/conftest.py`, impedindo poluição do ambiente real. |
| **USR-83** | Flaky: `test_runtime_spike_scenarios` R12 STORE_UNAVAILABLE | PR #129 (`c9d522e`) | `completed` | Isolamento de diretório SQLite por cenário, loops de retry com backoff exponencial e drenagem garantida de eventos terminais. |
| **USR-97** | Desacoplar manifesto vivo e validar invariantes de roadmap | PR #130 (`6d9573f`) | `completed` | Invariantes estruturais no teste do roadmap, desacoplamento de hashes de arquivos vivos e gate `test_live_file_hash_rule.py`. |
| **USR-95** | Workers xdist morrem com `0x8007000e` (E_OUTOFMEMORY) no notebook | PR #131 (`09463d8`) | `completed` | Dimensionamento dinâmico de workers xdist baseado em RAM livre (`tests/_worker_capacity.py`), bypass de WMI RPC via env var `PROCESSOR_ARCHITECTURE` e cap `--maxprocesses=8` no `pytest.ini`. |
| **USR-90** | Gate de drift de skills | PR #132 (`70e5ec6`) | `completed` | Gate `tests/test_skills_drift.py` para espelhamento estrito entre `.agents/skills` e `.claude/skills` e modos `--check`/`--help` em `scripts/sync_skills.py`. |
| **USR-101** | `catalog.sync_to_project` cria diretório e reporta sucesso em caminho inexistente | PR #133 (`d51bb10`) | `completed` | Exigência de `create=True` para criação de diretórios, resolução multiplataforma `paths: {windows, linux}` e proteção estrita contra path traversal. |
| **USR-102** | 74 caminhos rastreados e ignorados ao mesmo tempo | PR #134 (`a51d54e`) | `completed` | Remoção de regra excessiva no `.gitignore`, preservação dos relatórios versionados históricos, redução da allowlist para apenas os 2 arquivos do Telegram (USR-100) e uso de `state_root()` nos defaults de aceitação. |

---

## 3. Registro de Decisão sobre `core/harness` (USR-96)

Conforme as regras de governança inegociáveis (`guard.py` e `AGENTS.md`), nenhum arquivo dentro de `core/harness/*` pode ser alterado por agentes de IA.
Qualquer aprimoramento interno no formatador de veredito estruturado do `runner.py` (para incluir motivos de fallback detalhados no JSON de saída) fica formalmente catalogado como patch opcional exclusivo para commit manual do Owner sob o ticket **USR-96**.

---

## 4. Evidência Determinística de Validação

O portão oficial completo foi executado localmente no notebook com 100% de aprovação:
```powershell
python core/harness/runner.py --quick --local
```
- **Total de Testes Descobertos**: 2.947
- **Testes Aprovados**: 2.940
- **Testes Ignorados (Skipped justificados)**: 7
- **Falhas / Erros**: 0
- **Resultado Determinístico**: `[HARNESS_PASS]`
- **Estabilidade**: 0 flakiness observado, tempos de execução previsíveis e sem estouro de memória no Windows.
