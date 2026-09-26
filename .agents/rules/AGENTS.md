# Universal Engineering Standards (AGENTS.md)

Este documento governa a qualidade técnica de todo código produzido pelos agentes (Antigravity, Claude, Grok, DeepSeek, Qwen ou Ollama Local).

## 1. Padrões de Código e Arquitetura

- **Tipagem Estrita**: Todo código Python deve conter type hints completos; TypeScript deve operar com `strict: true`.
- **Desacoplamento de Visão & Lógica (Reachability)**:
  - NUNCA misture lógica de negócios com frameworks de apresentação (React, CLI prints, loops de jogo).
  - Toda regra de negócio deve ser acessível de forma headless por scripts automatizados (via chamadas de biblioteca ou APIs HTTP/CLI).
- **Tratamento de Erros e Logs**:
  - Proibido engolir exceções com `except: pass`.
  - Utilize structured logging com timestamps e contexto da operação.

## 2. Padrões de Teste e Validação

- **Test-Driven / Test-First**: Toda nova funcionalidade deve ser acompanhada de testes unitários ou de integração que falhem antes da implementação e passem depois.
- **Isolamento de Estado**: Testes não devem depender da ordem de execução nem deixar artefatos temporários órfãos no filesystem.
- **Portões Determinísticos**: A validação é decidida por comandos executáveis emitindo marcadores estruturados, nunca por um resumo conversacional de LLM.
- **Entrega com Operação no Mundo Real (Live End-to-End)**: Todo componente ou integração externa (ex: Telegram, n8n, GitHub, Dokploy, Provedores) só é considerado entregue após configuração ativa e validação operacional end-to-end no mundo real com o owner. É terminantemente proibido declarar entregas apenas com base em simulações/mocks sintéticos postergando a validação e setup real para o final.
- **Portão de Ambiguidade e Grill Mandatório (Gate G1)**: Nenhum agente pode avançar para o planejamento executável ou implementação de uma demanda aberta em linguagem natural sem antes submetê-la ao crivo de ambiguidade. Se houver lacunas materiais (canais, limiares, permissões, regras de negócio não especificadas), o agente deve obrigatoriamente pausar em `WAITING_HUMAN` com perguntas estruturadas de desambiguação até a manifestação do Owner. Inferir ou inventar parâmetros materiais sem validação é uma quebra estrita de governança.
- **Execução Canônica de Tickets e Roteamento Obrigatório (Skill 19-run-ticket)**: Toda demanda ou ticket do backlog deve ser executado seguindo a skill `19-run-ticket`. Antes de gerar código ou iniciar o PIV loop, o agente deve verificar a saúde de cota via `core.line.routing.pick('development')`. Se a conta do harness atual estiver com cota restante <= 15.0%, o agente é TERMINANTEMENTE PROIBIDO de implementar código com seu próprio modelo no chat interativo; deve recusar a demanda, informar a cota restante e delegar para o harness saudável eleito ou acionar o launcher headless `python C:\dev\DarkFac\run_ticket.py <TICKET_ID>`. A implementação em harness com cota <= 15.0% SÓ É PERMITIDA se o usuário exigir EXPLICITAMENTE no prompt (ex.: "forçar execução neste harness", "ignorar limite de cota", ou flag `--force`).


## 3. Padrões de Comunicação e Comandos para o Usuário

- **Comandos de Terminal para o Usuário**: SEMPRE indicar comandos no PowerShell/Terminal com o endereço absoluto completo (ex: `python C:\dev\DarkFac\run_canaletto.py`). O usuário pode abrir o shell a partir de qualquer pasta raiz (`C:\`, `C:\dev`, etc.); caminhos absolutos eliminam qualquer risco de `FileNotFoundError` ou ambiguidade de diretório.
- **Invocação Programática do Terminal no Windows**: Toda chamada interna ou subprocesso ao PowerShell DEVE incluir `-NoProfile -NonInteractive -ExecutionPolicy Bypass` para blindar o processo contra perfis globais que sequestram o diretório de trabalho. Utilize `core.harness.terminal_env.wrap_powershell_command` ou o script `scripts/init_terminal.ps1`.
- **Instruções de Configuração Manual (À Prova de Falhas e Retrabalho)**: Sempre que um passo envolver configuração manual pelo usuário (portais web, dashboards de provedores, Dokploy, Telegram BotFather, GitHub Settings, OAuth, consoles de nuvem, arquivos `.env`, formulários, etc.):
  - NUNCA assuma que o usuário tem experiência prévia na configuração ou sabe o que está fazendo.
  - Forneça instruções detalhadas passo a passo, tela por tela, refletindo com precisão a versão atual da interface da plataforma.
  - Forneça sugestões explícitas de conteúdo e preenchimento para absolutamente todos os campos que precisam ser preenchidos, seletores, dropdowns, toggles e checkboxes (valores recomendados, exemplos práticos ou exatamente o que colar).
  - O guia deve ser exaustivo, eliminando qualquer margem de ambiguidade para ser 100% à prova de falhas e de retrabalho.
- **Backups 100% Autônomos (Zero Toque Humano)**: É terminantemente proibido orientar o usuário a executar rotinas manuais de backup ou restore no terminal/PowerShell. A resiliência é operada de forma 100% autônoma pela Dark Factory através do daemon diário (`core.infra.backup_cron`), hooks pós-deploy (`scripts/dokploy_redeploy.py`), retenção assimétrica (7d R2 / 120d On-Prem) e restore drills automáticos em sandbox com alerta de emergência no Telegram em caso de falha.
