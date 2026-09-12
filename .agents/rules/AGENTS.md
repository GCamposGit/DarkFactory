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


## 3. Padrões de Comunicação e Comandos para o Usuário

- **Comandos de Terminal para o Usuário**: SEMPRE indicar comandos no PowerShell/Terminal com o endereço absoluto completo (ex: `python C:\dev\DarkFac\run_canaletto.py`). O usuário pode abrir o shell a partir de qualquer pasta raiz (`C:\`, `C:\dev`, etc.); caminhos absolutos eliminam qualquer risco de `FileNotFoundError` ou ambiguidade de diretório.
- **Invocação Programática do Terminal no Windows**: Toda chamada interna ou subprocesso ao PowerShell DEVE incluir `-NoProfile -NonInteractive -ExecutionPolicy Bypass` para blindar o processo contra perfis globais que sequestram o diretório de trabalho. Utilize `core.harness.terminal_env.wrap_powershell_command` ou o script `scripts/init_terminal.ps1`.
- **Instruções de Configuração Manual (À Prova de Falhas e Retrabalho)**: Sempre que um passo envolver configuração manual pelo usuário (portais web, dashboards de provedores, Dokploy, Telegram BotFather, GitHub Settings, OAuth, consoles de nuvem, arquivos `.env`, formulários, etc.):
  - NUNCA assuma que o usuário tem experiência prévia na configuração ou sabe o que está fazendo.
  - Forneça instruções detalhadas passo a passo, tela por tela, refletindo com precisão a versão atual da interface da plataforma.
  - Forneça sugestões explícitas de conteúdo e preenchimento para absolutamente todos os campos que precisam ser preenchidos, seletores, dropdowns, toggles e checkboxes (valores recomendados, exemplos práticos ou exatamente o que colar).
  - O guia deve ser exaustivo, eliminando qualquer margem de ambiguidade para ser 100% à prova de falhas e de retrabalho.
