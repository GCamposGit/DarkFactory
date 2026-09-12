---
name: prime-intelligence
description: Mapeia e ingere a estrutura de qualquer repositório (arquitetura, stack, dependências, padrões de código e convenções) em minutos usando o motor Gemini 3.8 Flash para contextos massivos e subagentes paralelos. Use quando iniciar o trabalho em uma nova codebase ou ao receber um ticket de grande escopo.
---

# 01 - Prime Intelligence: Ingestão e Reconhecimento de Codebase

Esta skill orienta o agente em uma base de código existente, extraindo seus padrões essenciais de arquitetura, contratos e convenções de engenharia sem sobrecarregar a memória de trabalho.

---

## 1. Contratos Normativos da Etapa

### Inputs (Entradas)
- **Raiz do Repositório**: Caminho absoluto do workspace e verificação de CWD.
- **Arquivos de Governança**: `MISSION.md`, `FACTORY_RULES.md`, `AGENTS.md`.
- **Manifestos de Dependências**: `pyproject.toml`, `requirements.txt`, `package.json`, `Cargo.toml`, etc.
- **Histórico e Preferências**: `.factory/learning/learning_ledger.json`.

### Ações e Procedimento Executável
1. **Preflight de Terminal e Ancoragem Defensiva de CWD**:
   - Assegure que o processo opere na raiz do projeto (`core.harness.terminal_env` ou `scripts/init_terminal.ps1`).
   - Em invocações PowerShell no Windows, utilize SEMPRE `-NoProfile -NonInteractive -ExecutionPolicy Bypass` para blindagem contra perfis globais que sequestram o diretório.
2. **Detecção de Stack e Desacoplamento Headless (Reachability)**:
   - Identifique pontos de entrada (*entrypoints*), camada de domínio puro e suítes de testes.
   - Verifique que a lógica de negócio é alcançável de forma headless (via biblioteca, CLI ou HTTP).
3. **Extração de Convenções e Anti-Padrões**:
   - Registre estilo de nomenclatura, injeção de dependências, tratamento de exceções e utilitários internos existentes.

### Outputs Estruturados
- **Relatório de Inteligência**: `.factory/context_intelligence.json` estruturado com:
  - `stack`: linguagem, runtime, framework, test runner.
  - `reachability`: driver (`library`, `cli`, `http`), comandos oficiais de teste.
  - `governance_detected`: confirmação dos contratos do repositório.
- **Insumos de Ambiente**: Dados para composição do futuro `EnvironmentManifest` da etapa de planejamento.

### Portões, Política e Validação
- **Fase Estritamente Read-Only**: O mapeamento de inteligência não introduz alterações em arquivos de código de produção.
- **Subordinação à Governança**: Conflitos entre convenções locais e o `AGENTS.md` são registrados para auditoria, sem autoaprovação de exceções.
- **Modelos Recomendados**:
  - Orquestração & Ingestão Massiva: `gemini-3.8-flash` (Antigravity Native) ou modelo de contexto longo qualificado.
  - Pesquisa Externa & Docs Vivos: `grok-4.6` (xAI API).
  - Varredura Local Rápida: `qwen-fast:latest` (Ollama Local, $0).

---

## 2. Continuous Self-Improvement & RCA de Mapeamento

- **Prevenção de Ambiguidade**: Detecte convenções tácitas já presentes no repositório antes de sugerir qualquer arquitetura.
- **RCA em Falhas de Descoberta**: Se entrypoints ou suítes não forem detectados, execute RCA via `python -m core.learning.cli rca` para catalogar diretórios não padronizados e atualizar o `context_intelligence.json`.
