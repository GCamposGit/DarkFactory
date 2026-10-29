# Contrato dos Agentes da DarkFac

Fonte única das regras para todo agente e harness (Claude Code, Codex, Grok Build, Antigravity, workers headless e subagentes). `CLAUDE.md` e `.agents/rules/GEMINI.md` apenas importam este arquivo; nenhuma regra é repetida em outro lugar.

- Missão de longo prazo: [`MISSION.md`](MISSION.md).
- Contexto da fase atual (canais, máquinas, orçamento, metas, piloto): [`docs/FASE_ATUAL.md`](docs/FASE_ATUAL.md). Consulte antes de decidir rota, gasto, host ou quando acionar o owner.
- Plano vigente: [`docs/plans/PLANO_V0_2026-10-24.md`](docs/plans/PLANO_V0_2026-10-24.md). Interoperabilidade de harnesses: [`docs/HARNESS_INTEROP.md`](docs/HARNESS_INTEROP.md).

## 1. Como a fábrica evolui

1. **Processo no código.** Regra que importa vira verificação em código (guard, portão, hook, motor); este texto só aponta onde ela vive.
2. **Um caminho só.** Toda demanda, de qualquer canal, passa por `run_ticket.py` (skill `19-run-ticket`) e pelo mesmo registro.
3. **Saldo de código.** Ajuste e reaproveite antes de criar; o núcleo deve encolher com o tempo, salvo funcionalidade nova pedida pelo owner.
4. **Orçamento de testes.** Teste novo de comportamento substitui ou consolida testes existentes; teste de forma (texto, contagem, presença de string em doc) não entra no portão.
5. **Owner só para o que é dele.** Objetivo, critério de sucesso, decisão de negócio, segredo novo e aceite. Todo o resto a fábrica resolve.
6. **Documentação a cada entrega.** Manual (`docs/manual/`), `docs/FASE_ATUAL.md` e learning pack são atualizados na mesma entrega que muda o fato.

## 2. Intake e Gate G1

- Demanda em linguagem natural com ambiguidade material (canais, limiares, permissões, regras de negócio) pausa em `WAITING_HUMAN`.
- Faça o Grill estruturado (perguntas com opção recomendada e impacto) e espere a decisão explícita do owner antes de planejar ou codificar.
- Nunca invente parâmetros nem trate respostas de ensaios anteriores como autorização para uma demanda nova.

## 3. Execução de tickets e cota

- Todo ticket segue a skill `19-run-ticket`; o launcher headless é `python C:\dev\DarkFac\run_ticket.py <TICKET_ID>`.
- **Preflight de cota** antes de planejar ou implementar:
  ```powershell
  python -c "from core.line.routing import pick; print(pick('development', ['harness:claude', 'harness:codex', 'harness:grok', 'harness:antigravity']))"
  ```
- **Piso de 15%**: com cota restante do próprio harness `<= 15%` (semanal ou janela móvel), o agente não implementa código com o próprio modelo no chat. Informa a cota e delega ao harness eleito ou ao launcher. Só o roteador elege `development`, e só entre harnesses com modo `write` (Claude, Codex, Grok Build; Antigravity escreve só quando é o harness de operação interativo, nunca headless ou na frota).
- **Override**: abaixo do piso, só com pedido textual inequívoco do owner no prompt (ex.: "forçar execução neste harness", "ignorar limite de cota", `--force`). Sem isso, falhe fechado.
- **Harness de operação**: o harness pelo qual o owner opera (`DARKFAC_OPERATING_HARNESS` ou autodetecção) é eleito para `development` enquanto estiver acima do piso; abaixo dele vale o Dynamic Headroom. Não altera grill, planning, review nem integration.
- **Modelos**: ids concretos vivem só em `.factory/config/model_registry.json` (lido por `core.models.registry`); textos citam apenas aliases. Planejamento usa o alias `opus`; implementação por subagente usa `sonnet`.
- **Subagentes do chat gastam a cota do chat.** Antes de paralelizar, confira a cota; com a janela pressionada, mande o trabalho para a frota (`run_ticket.py --create --queue-only`).
- **Revisão cruzada**: o revisor é sempre de família de modelo diferente da do implementador.

## 4. Código

- Python 3.12+, type hints em APIs públicas, `snake_case`, `pathlib.Path`, `logging` estruturado, UTF-8 explícito na entrada/saída de CLI; sem emoji sem fallback no Windows.
- Pydantic v2 para contratos; FastAPI só na camada HTTP. Regra de negócio fica em `core/` ou serviços, acessível headless (biblioteca, CLI ou HTTP), nunca em UI.
- Nada de `except: pass`. Falha externa (Ollama, OpenRouter, GitHub, arXiv, Dokploy) vira fallback ou erro estruturado, sem vazar credencial.
- Segredos nunca entram no repositório; `.env.example` sem valores reais.
- Dependência nova atualiza `requirements.txt` (ou `requirements-audio.txt`) e o bootstrap.
- Não altere nem versione o experimento Canaletto.
- `.agents/skills/` é a fonte das skills; `.claude/skills/` é espelho via `python scripts/sync_skills.py`.

## 5. Validação

Portão único oficial:

```powershell
python C:\dev\DarkFac\core\harness\runner.py --quick
```

- Roda a suíte inteira em paralelo (`pytest-xdist`), com verdict cacheado por árvore e `suite_lock` por máquina. Não rode `pytest tests` como segundo portão nem portões em paralelo no mesmo host.
- Sucesso só com marcador `[HARNESS_PASS]` e ao menos uma checagem executada.
- Loop interno: `python -m pytest tests/test_meu_modulo.py -q` ou `python -m core.harness.affected --run`. Nunca substitui o portão.
- Antes de abrir PR, rode o que o CI exige: `lint-imports`, `python scripts/complexity_ratchet.py` e `python -m core.harness.docs_sync --base origin/main --head HEAD`.
- Detalhes de workers, hosts e variáveis em `docs/HARNESS_INTEROP.md`.

## 6. Entrega autônoma (zero toque humano)

Em projetos internos (`project: darkfac`) o ciclo termina sem pedir nada ao owner, em qualquer harness:

1. Commit atômico (`feat(...): ... [TICKET_ID]`) em branch/worktree própria.
2. Push, PR (`gh pr create`) e resolução de conflito com `origin/main` pelo próprio agente.
3. Merge squash depois do CI verde e confirmação do sha em `origin/main`.
4. Limpeza de worktree e branch; sincronização dos ambientes via `core.git.autonomy`.
5. Deploy pós-merge: `python C:\dev\DarkFac\scripts\dokploy_redeploy.py` (detalhes em `docs/runbooks/dokploy_redeploy.md`); relate o resultado por serviço.
6. Ticket marcado `completed` com evidência.

- É proibido encerrar o turno pedindo push, revisão de portão, autorização de merge ou de deploy. Ferramenta negada por permissão: reporte a negação exata e a regra a ajustar.
- Não faça merge com portão falho, suíte não descoberta ou governança alterada sem autorização assinada.
- Única exceção: projeto com `requires_commercial_acceptance: true` (cliente pago em produção; hoje nenhum).
- Backups e restauração são autônomos (`core.infra.backup_cron`, hooks pós-deploy); nunca peça rotina manual de backup.

## 7. Defeito encontrado

Todo erro, bug, teste instável, regressão, inconsistência de configuração ou lacuna de processo identificado por qualquer agente é, no mesmo turno:

1. corrigido na hora dentro do ciclo autônomo, se for pequeno e seguro; ou
2. registrado como ticket formal (`status: planned`, problema, evidência, critérios de aceite) via `python C:\dev\DarkFac\run_ticket.py --create --queue-only ...` ou `core.demands`.

Nunca só mencione o problema. O relatório do turno lista cada defeito com o ticket ou o commit/PR.

## 8. Ações do owner e comunicação

- Ação que só o owner pode executar (segredo, portal, commit em protegido, decisão, aceite) vira, no mesmo turno, item `OA-NNN` em `.factory/owner_actions/owner_actions.json` (exibido no DarkHub): título, prioridade, por quê, tickets bloqueados, passo a passo tela a tela com comandos absolutos, critério de verificação e, se for decisão, as opções com a recomendada. Quem resolve marca `done` com `resolved_at`. O chat repete o passo a passo pronto.
- Configuração manual: nunca assuma experiência; instrua tela por tela na interface atual e sugira valor para cada campo, seletor e toggle.
- Comandos para o owner: PowerShell com caminho absoluto (`python C:\dev\DarkFac\run_ticket.py USR-01`).
- Subprocesso PowerShell interno: sempre `-NoProfile -NonInteractive -ExecutionPolicy Bypass` (ou `core.harness.terminal_env`).
- Nada é entregue só com mock: integração externa conta como entregue após validação real de ponta a ponta.

## 9. Governança protegida

- Protegidos: a lista vive em `PROTECTED_PATTERNS` de `core/orchestrator/guard.py` (hoje `MISSION.md`, `AGENTS.md`, `FACTORY_RULES.md`, o guard, `core/governance/*`, a chave pública do owner e `.github/workflows/*`).
- A fábrica pode propor mudanças neles, mas elas só entram com autorização assinada pelo owner em `.factory/governance/authorizations/` (registro com ticket, paths exatos e sha do candidato; o owner roda `python -m core.governance.keys sign <arquivo>`). Não faça `update-branch` depois de assinado: muda o sha.
- `docs/FASE_ATUAL.md` não é protegido.

## 10. Harnesses

- Claude Code: `CLAUDE.md` importa este arquivo; skills em `.claude/skills/`.
- Antigravity: `.agents/rules/GEMINI.md` importa este arquivo; skills em `.agents/skills/`; elegível para `development` só como harness de operação interativo; somente leitura em modo headless e na frota.
- Codex, Grok e outros: leem este arquivo na raiz do repositório.
