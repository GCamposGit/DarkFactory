# DarkFac Factory Rules

Estas regras valem para Codex, Antigravity, Grok, Claude Code e qualquer outro harness que opere neste repositório.

1. Leia `MISSION.md` e `AGENTS.md` antes de alterar código.
2. Trate `.agents/skills/` como fonte canônica; mantenha `.claude/skills/` sincronizado com `python scripts/sync_skills.py` quando houver mudança de skill.
3. Nunca grave segredos no repositório. Use variáveis de ambiente e mantenha `.env.example` sem valores reais.
4. Não altere, adicione ou versione o experimento Canaletto. Ele é local por decisão de escopo.
5. Toda mudança deve passar por `python core/harness/runner.py --quick`. Esse comando já executa a suíte inteira em `tests/` (paralela via `pytest-xdist`, cacheada por árvore de commit, enfileirada por máquina via `suite_lock`); não existe um segundo comando `pytest` separado a rodar como portão — isso duplicava a validação e causava timeouts quando mais de um harness validava no mesmo host simultaneamente. Para iteração local antes do portão, use testes focados ou `python -m core.harness.affected --run`.
6. Um harness só pode declarar sucesso se houver marcadores determinísticos `[HARNESS_PASS]` e pelo menos uma checagem executada.
7. Preserve compatibilidade headless: regras de negócio ficam em `core/` ou serviços, não em handlers de UI.
8. Prefira caminhos relativos ao repositório, `pathlib`, UTF-8 explícito e comandos equivalentes em PowerShell e POSIX.
9. Não faça merge automático quando um portão falhar, quando a suíte não puder ser descoberta ou quando a mudança tocar governança sem justificativa.
10. Mudanças de dependências devem atualizar `requirements.txt` (e `requirements-audio.txt` para o stack opcional de GPU) e a documentação de bootstrap.
11. Autonomia Total de Git em Projetos Internos (Zero Toque Humano Pós-Grill, USR-57): Em desenvolvimentos do núcleo da fábrica e ferramentas internas (`project: darkfac`), a Dark Factory possui autonomia plena para criar branches, comitar alterações atômicas vinculadas a tickets, validar no portão oficial e sincronizar via push/merge para `origin/main` sem exigir aprovações humanas manuais. O ciclo de vida do ticket (Skill 19 / `run_ticket.py`) é encerrado de ponta a ponta pelo harness/agente. A exigência de aprovações manuais pré-merge restringe-se exclusivamente a projetos comerciais com flag `requires_commercial_acceptance: true` onde há clientes externos em produção real.
12. Sincronização Contínua Multi-Ambiente (USR-57): Ao concluir qualquer ticket ou receber novos commits validados, os ambientes da fábrica (Desktop worker, VPS Dokploy e Notebook de dev) devem ser sincronizados via `core.git.autonomy` (`git fetch` + fast-forward ou push), assegurando que o próximo harness trabalhe sempre com a árvore limpa e no commit mais recente sem divergência.

