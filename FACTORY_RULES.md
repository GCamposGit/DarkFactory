# DarkFac Factory Rules

Estas regras valem para Codex, Antigravity, Grok, Claude Code e qualquer outro harness que opere neste repositório.

1. Leia `MISSION.md` e `AGENTS.md` antes de alterar código.
2. Trate `.agents/skills/` como fonte canônica; mantenha `.claude/skills/` sincronizado com `python scripts/sync_skills.py` quando houver mudança de skill.
3. Nunca grave segredos no repositório. Use variáveis de ambiente e mantenha `.env.example` sem valores reais.
4. Não altere, adicione ou versione o experimento Canaletto. Ele é local por decisão de escopo.
5. Toda mudança deve passar por `python core/harness/runner.py --quick`; alterações de comportamento também devem passar por `python -m pytest tests -v --ignore=tests/test_canaletto.py`.
6. Um harness só pode declarar sucesso se houver marcadores determinísticos `[HARNESS_PASS]` e pelo menos uma checagem executada.
7. Preserve compatibilidade headless: regras de negócio ficam em `core/` ou serviços, não em handlers de UI.
8. Prefira caminhos relativos ao repositório, `pathlib`, UTF-8 explícito e comandos equivalentes em PowerShell e POSIX.
9. Não faça merge automático quando um portão falhar, quando a suíte não puder ser descoberta ou quando a mudança tocar governança sem justificativa.
10. Mudanças de dependências devem atualizar `requirements.txt` (e `requirements-audio.txt` para o stack opcional de GPU) e a documentação de bootstrap.
11. Ticket de desenvolvimento só pode ser declarado concluído após commit revisado, push da branch, PR no GitHub, checks/reviews exigidos, merge confirmado e verificação de que o `main` remoto alcança o SHA entregue; falha de autenticação, rede ou leitura do remoto é bloqueio explícito.
