<!-- covers: core/learning_pack/delivery.py -->
# Learning pack da entrega

Gerado por codigo, a partir do diff `base..head`, dos testes alterados e do ADR (se houver). Tem dois niveis: leigo (o que mudou, em linguagem simples) e dev (arquivos, funcoes, testes).

Uso:

```powershell
python -m core.learning_pack.delivery --base origin/main --head HEAD --ticket USR-XXX --out C:\caminho\saida
```

Gera `<ticket>.json` (schema Pydantic `LearningPackArtifact`) e `<ticket>.md`.

Verificacao: se um redator (`llm`) for injetado em `build_for_delivery`, cada frase que cita caminho ou simbolo e conferida contra o diff e a arvore do head. Frase com arquivo inexistente, arquivo nao alterado citado como alterado ou simbolo inexistente e removida e registrada em `rejected_claims`. Sem `llm`, o texto vem so dos fatos.

## Hook Stop (USR-401)

`scripts/stop_hook_artifacts.py`, registrado em `.claude/settings.json` (hooks.Stop), impede o Claude Code de encerrar quando o diff contra `origin/main` (ou a worktree suja) toca `core/`, `hub/`, `scripts/`, `tests/` ou `run_ticket.py` e a branch/ultimo commit cita `USR-NNN`, sem `.factory/learning_packs/USR-NNN.md` (ou `.json`). Bloqueia com exit 2 e o comando de geracao em stderr. Com `stop_hook_active` ou falha inesperada (git ausente etc.) libera com exit 0.

## Aprendizado da entrega USR-456 T2

O preflight continua consultando a saúde do grokbox, mas o wake por email só ocorre com autorização
explícita propagada pelo launcher. Worktrees ligadas ao mesmo checkout compartilham o estado em
`.factory/grokbox_wake/state.json`; `DARKFAC_STATE_ROOT` explícito mantém seu próprio destino. Um lock
entre processos protege a decisão, reserva antes do envio e gravação do timestamp. Assim, até respostas
ambíguas do provedor ficam limitadas a uma tentativa por janela de 30 minutos. Causas estruturadas de
supressão incluem `external_communication_not_authorized`, `throttle_active`,
`shared_state_root_unavailable`, `state_unreadable`, `state_write_failed` e `lock_unavailable`; essas
falhas não interrompem a sondagem nem o launcher.
