# Claude Code Contract (CLAUDE.md)

Este documento governa a operação do Claude Code no repositório DarkFac, em estrita conformidade com `AGENTS.md`, `MISSION.md` e `FACTORY_RULES.md`.

---

## 1. Regra Inviolável de Proteção de Quota e Execução de Tickets (Skill 19-run-ticket)

- **Preflight Obrigatório de Cota**:
  Antes de iniciar o planejamento, refatoração ou implementação de qualquer ticket, verifique a saúde das cotas do ecossistema:
  ```powershell
  python -c "from core.line.routing import pick; print(pick('development', ['harness:claude', 'harness:codex', 'harness:grok', 'harness:antigravity']))"
  ```
- **Bloqueio de Quota Crítica no Chat (Fail-Closed <= 15.0%)**:
  Se a conta do Anthropic Claude estiver com cota restante $\le 15.0\%$ (semanal ou janela móvel), o Claude Code é **TERMINANTEMENTE PROIBIDO** de implementar código com seu próprio modelo no chat interativo.
  O agente deve:
  1. Recusar a implementação local no chat;
  2. Informar ao usuário que a cota do Claude está em estado crítico ($\le 15\%$);
  3. Indicar o harness saudável eleito com maior margem (atualmente Antigravity) ou acionar o launcher headless canônico:
     ```powershell
     python C:\dev\DarkFac\run_ticket.py <TICKET_ID>
     ```
- **Exceção de Override Explícito pelo Usuário**:
  A implementação com cota $\le 15.0\%$ **SÓ É PERMITIDA se o usuário exigir EXPLICITAMENTE no prompt** (ex.: *"forçar execução no Claude"*, *"ignorar limite de cota"*, *"estou ciente da cota crítica, prossiga"* ou flag `--force`). Sem essa instrução textual inequívoca, o agente deve falhar fechado (*fail-closed*).

---

## 2. Padrões de Código e Convenções

- **Python**: 3.12+, type hints estritos em APIs públicas e nomes `snake_case`.
- **Validação de Dados**: Pydantic v2 para contratos; FastAPI exclusivamente na camada HTTP.
- **I/O e Filesystem**: Utilize `pathlib.Path` para caminhos e `logging` estruturado para diagnóstico.
- **Blindagem do Windows**: UTF-8 na entrada/saída de CLI. Não imprima emojis sem fallback seguro.

---

## 3. Padrões de Comunicação e Comandos para o Usuário

- **Comandos de Terminal para o Usuário**: SEMPRE indicar comandos no PowerShell com o endereço absoluto completo (ex.: `python C:\dev\DarkFac\run_ticket.py USR-01`).
- **Invocação Programática do Terminal no Windows**: Toda chamada interna ou subprocesso ao PowerShell DEVE incluir `-NoProfile -NonInteractive -ExecutionPolicy Bypass`.
- **Instruções de Configuração Manual**: Nunca assuma conhecimento prévio do usuário. Forneça instruções passo a passo, tela por tela, sugerindo valores e opções recomendadas para todos os campos e seletores.
- **Autonomia Total de Git em Projetos Internos (Zero Toque Humano Pós-Grill, USR-57)**: É expressamente proibido orientar o usuário a executar commits, merges ou rotinas manuais de sincronização Git no terminal. Em projetos internos (`project: darkfac`), o Claude Code e a fábrica realizam o ciclo completo de forma 100% autônoma (commit atômico, merge e sincronização com `origin/main` via `core.git.autonomy` / `run_ticket.py`), atualizando a demanda para `completed`. Aprovações manuais pré-merge restringem-se exclusivamente a projetos comerciais com `requires_commercial_acceptance: true`.


---

## 4. Intake e Portão de Ambiguidade (Gate G1)

- Toda nova demanda em linguagem natural que possua ambiguidades materiais (canais, limiares, permissões, regras de negócio) exige pausa imediata em `WAITING_HUMAN`.
- O agente nunca deve assumir parâmetros ou iniciar código antes de executar o Grill estruturado e receber as decisões explícitas do Owner.

---

## 5. Validação Obrigatória (Portão Único)

```powershell
python C:\dev\DarkFac\core\harness\runner.py --quick
```

Este é o único portão oficial. Executa a suíte completa em paralelo via `pytest-xdist` com verificação de locks e cache de verdicts. Não execute `pytest tests -v` separadamente como segundo portão.

Para iteração rápida local antes do portão final:
```powershell
python -m pytest tests/test_meu_modulo.py -q
```

---

## 6. Deploy pós-merge

Depois que qualquer mudança chegar em `main`, execute:
```powershell
python C:\dev\DarkFac\scripts\dokploy_redeploy.py
```
