# Arquitetura da DarkFac

Visão enxuta da fábrica **atual**, conferida contra o código em 27/10/2026. Detalhes por tema vivem em [`docs/manual/`](manual/); regras dos agentes em [`AGENTS.md`](../AGENTS.md); contexto da fase (hosts, canais, orçamento) em [`FASE_ATUAL.md`](FASE_ATUAL.md); plano vigente em [`plans/PLANO_V0_2026-10-24.md`](plans/PLANO_V0_2026-10-24.md). Planos substituídos ficam em `docs/archive/plans/`.

## 1. Fluxo de ponta a ponta

```text
canal (chat, Telegram, Hub)  ->  fila (control store + demands.json)
        -> despachante da frota  -> run_ticket.py (motor único) em um host
        -> portão (runner.py) -> revisão cruzada -> PR + CI -> merge squash
        -> deploy do Hub (GHCR + blue-green) -> ticket `completed` com evidência
```

Todo canal passa pelo mesmo caminho e pelo mesmo registro.

## 2. Motor único: `run_ticket.py` e `core/line/`

- `run_ticket.py` é o launcher (skill `19-run-ticket`): cria tickets (`--create --queue-only`), prepara a worktree, escolhe o harness, roda os estágios e entrega.
- Os estágios canônicos estão em `core/line/stages.py` (`Stage`): INTAKE, GRILL, PLAN, RESEARCH, BUILD, VALIDATE, REVIEW, MERGE, DEPLOY_SMOKE, DOCS, LEARNING. Cada um deixa um artefato em `.darkfac/runs/<run_id>/`, validado por `require_artifact`.
- Implementação por estágio em `core/line/stage_*.py` (grill, planning, build, review, integration, release); orquestração em `ticket_engine.py` e `ticket_stages.py`; worktrees em `workspace.py`; reserva por host em `line_lock.py`.
- Ambiguidade material em linguagem natural pausa em `WAITING_HUMAN` (Gate G1, `core/demands/grill.py`).
- Persistência de workflow em `core/workflow/` (`control_store.py`, `readiness.py`); ver [`manual/arquitetura.md`](manual/arquitetura.md) e [`manual/motor.md`](manual/motor.md).

## 3. Fila

- Fonte de verdade: control store (`core/demands/queue_store.py`): Postgres se `DARKFAC_HF02_DATABASE_URL` estiver definida, senão SQLite local `control.db`. Tickets entram por `run_ticket.py --create --queue-only` ou pelo intake do Telegram, sem worktree nem PR.
- `.factory/demands/demands.json` é o ledger versionado (histórico e fallback); `queue_store.merge_tickets` une as duas fontes. Ver [`manual/fila.md`](manual/fila.md).
- Ações só do owner vivem em `.factory/owner_actions/owner_actions.json` (itens `OA-NNN`, exibidos no Hub).

## 4. Despachante da frota

- `core/line/fleet_dispatcher.py` (`--loop` / `--once --dry-run`) roda sem sessão de chat: lê `demands.json` de `origin/main`, escolhe tickets `planned` executáveis e despacha **um por host livre** com `run_ticket.py --fleet --host-id <host>`.
- Topologia em `.factory/config/line_fleet.json`: `grokbox` (prioridade 1), `notebook` (2), `desktop` (3), um slot cada, máximo 3 simultâneos.
- `DARKFAC_FLEET_DISABLED_HOSTS` (ids separados por vírgula) tira hosts da frota sem editar o JSON.
- Falha de infra reenfileira o ticket (continua `planned`, com cooldown). Ver [`manual/frota.md`](manual/frota.md).

## 5. Roteamento por cota

- `core/line/routing.py` (`pick`) elege o harness por etapa entre Claude, Codex, Grok Build e Antigravity (este só leitura), usando a cota restante de cada conta e a configuração em `.factory/config/line_routing.json`.
- **Piso de 15%**: com cota restante `<= 15%`, ou cota desconhecida/vencida, o harness não é eleito para escrever código; sem override textual do owner, falha fechado.
- Modelos são resolvidos por alias em `.factory/config/model_registry.json` (`core/models/registry.py`). Revisão sempre por família de modelo diferente da do implementador. Ver [`manual/roteamento.md`](manual/roteamento.md).

## 6. Portão e CI

- Portão local único: `python core/harness/runner.py --quick` (suíte em paralelo com `pytest-xdist`, verdict cacheado por árvore, `suite_lock` por máquina, workers remotos via `remote_dispatch.py`). Sucesso exige `[HARNESS_PASS]` (`markers.py`). Ver [`manual/portao.md`](manual/portao.md).
- CI (`.github/workflows/ci.yml`): `pr-tests` roda guard de governança, `lint-imports`, `scripts/complexity_ratchet.py`, `core.harness.docs_sync` e `runner.py --profile pr`; há validação em Ubuntu e Windows; `main-tests` e `continuous-deployment` rodam no `main`. O merge exige CI obrigatório (ruleset do `main`).

## 7. Deploy do Hub

- O Hub é publicado como imagem no GHCR por sha (`.github/workflows/hub-image.yml`) e implantado em **blue-green** no Traefik (`core/orchestrator/bluegreen.py`).
- `scripts/dokploy_redeploy.py` é o passo pós-merge único; espera a imagem GHCR do sha e reporta o resultado por serviço. Runbook: [`runbooks/dokploy_redeploy.md`](runbooks/dokploy_redeploy.md).
- `core/infra/deploy_classifier.py` decide se o merge exige rebuild: só caminhos embutidos na imagem (`core/`, `hub/`, `scripts/`, `deploy/`, `.factory/config/`, `requirements.txt`, `harness.config.json`) disparam; docs, testes e ledger não. Diff desconhecido ou vazio sempre reconstrói (fail closed).

## 8. DarkHub

- `hub/backend/` (FastAPI: `api.py`, `service_*.py`) e `hub/frontend/`. Mostra a fila, a frota, a esteira ao vivo (`/live`), custo, cotas e as intervenções do owner (`/api/interventions/priority`).
- Regra de negócio fica em `core/`; o Hub só expõe e apresenta.

## 9. Governança protegida

- `core/orchestrator/guard.py` (`PROTECTED_PATTERNS`) bloqueia alterações de agentes em `MISSION.md`, `AGENTS.md`, `FACTORY_RULES.md`, no próprio guard, `core/governance/*`, na chave pública do owner e em `.github/workflows/*`.
- Mudanças nesses caminhos só entram com autorização assinada pelo owner em `.factory/governance/authorizations/`. Ver [`manual/governanca.md`](manual/governanca.md).

## 10. Skills e harnesses

- `.agents/skills/` é a fonte; `.claude/skills/` é espelho (`python scripts/sync_skills.py`). Claude Code importa `AGENTS.md` via `CLAUDE.md`; Antigravity via `.agents/rules/GEMINI.md`; Codex e Grok leem `AGENTS.md` na raiz. Ver [`HARNESS_INTEROP.md`](HARNESS_INTEROP.md).

## 11. Vertical de referência: Echo Garden

`core/game/` isola o domínio determinístico do jogo (estado + movimento, sem rede, relógio ou I/O) das integrações de modelo e imagem. Plano e critérios: [`DARK_FACTORY_E2E_TEST_PLAN.md`](DARK_FACTORY_E2E_TEST_PLAN.md).
