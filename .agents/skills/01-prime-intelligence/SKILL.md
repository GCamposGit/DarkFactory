---
name: prime-intelligence
description: Mapeia e ingere a estrutura de qualquer repositório (arquitetura, stack, dependências, padrões de código e convenções) em minutos usando o motor Gemini 3.8 Flash para contextos massivos e subagentes paralelos. Use quando iniciar o trabalho em uma nova codebase ou ao receber um ticket de grande escopo.
---

# Prime Intelligence: Ingestão e Reconhecimento de Codebase

Esta skill orienta o agente em uma base de código existente, extraindo os padrões essenciais sem sobrecarregar a memória de trabalho.

## Modelo Recomendado
- **Primário**: `gemini-3.8-flash` (Antigravity Native) — ingestão de 1M-2M tokens com custo de leitura irrisório e alta velocidade.
- **Pesquisa Externa**: `grok-4.6` — para buscar documentações e issues recentes de bibliotecas de terceiros.
- **Local Fallback**: `qwen-fast:latest` (Ollama) — para varredura de árvores de diretórios e regex rápido local.

## Procedimento Passo a Passo

### 0. Preflight do Ambiente de Terminal e Ancoragem de CWD
Antes de qualquer leitura ou inventário inicial:
- Assegure que o diretório de trabalho do processo corresponde exatamente à raiz do projeto (`ensure_clean_working_directory()` ou executando `scripts/init_terminal.ps1`).
- Se invocar comandos no Windows via PowerShell, SEMPRE utilize as flags defensivas `-NoProfile -NonInteractive -ExecutionPolicy Bypass` para evitar que perfis globais do sistema (`Microsoft.PowerShell_profile.ps1`) sequestrem o diretório de trabalho para `C:\dev`.
- Se a primeira leitura falhar, diagnostique e reancore o terminal imediatamente via `python core/harness/terminal_env.py --fix` em vez de fragmentar chamadas.

### 1. Detecção de Stack e Ecossistema
Identifique os arquivos raiz de configuração:
- Python: `pyproject.toml`, `requirements.txt`, `setup.py`
- Node/TS: `package.json`, `tsconfig.json`
- Rust/Go: `Cargo.toml`, `go.mod`
- Governança: `MISSION.md`, `FACTORY_RULES.md`, `AGENTS.md`, `CLAUDE.md`

### 2. Mapeamento de Arquitetura em Três Camadas
Execute uma inspeção estruturada:
1. **Ponto de Entrada (Entrypoints)**: Onde a aplicação inicializa? (ex: `main.py`, `index.ts`, `server.go`).
2. **Camada de Domínio / Lógica**: Onde residem as regras de negócio puras (desacopladas de frameworks visuais)?
3. **Superfície de Testes**: Como os testes são executados? Qual o comando oficial (`pytest`, `npm test`, `cargo test`)?

### 3. Extração de Convenções e Anti-Padrões
Registre:
- Estilo de nomenclatura (camelCase, snake_case, PascalCase).
- Padrão de injeção de dependências e tratamento de erros.
- Bibliotecas internas utilitárias já existentes para evitar recriação de "rodas".

### 4. Geração do Relatório de Inteligência
Gere ou atualize o artefato `.factory/context_intelligence.json`:
```json
{
  "stack": {
    "language": "python 3.12",
    "framework": "fastapi",
    "test_runner": "pytest"
  },
  "reachability": {
    "driver": "http",
    "test_command": "pytest tests/ -v"
  },
  "governance_detected": true
}
```

---

## 🧠 Continuous Self-Improvement & Failure RCA Integration

1. **Prevenção de Ambiguidade para Execução One-Shot**:
   - Durante o mapeamento inicial, detecte não apenas arquivos, mas as convenções tácitas e padrões preferidos do usuário já presentes no repositório.
   - Consulte `.factory/learning/learning_ledger.json` para carregar preferências do usuário registradas anteriormente antes de sugerir ou planejar qualquer arquitetura.
2. **Root Cause Analysis (RCA) em Falhas de Mapeamento**:
   - Se o agente falhar em localizar entrypoints, drivers ou testes, execute RCA: a falha decorreu de caminhos não padronizados, imports dinâmicos ou scripts ocultos?
   - Registre a causa raiz via `python core/learning/cli.py rca` e codifique o caminho descoberto no `context_intelligence.json` para que o erro nunca se repita.
3. **Extrapolação Analógica**:
   - Quando um padrão de injeção ou tratamento de erro for detectado em um módulo (ex: `core/audio/`), projete a mesma convenção para todos os novos módulos a serem criados no repositório.

