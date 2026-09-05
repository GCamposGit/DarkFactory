# FACTORY_RULES.md — Regras de Operação Autônoma (Nível 3)

Regras inquebráveis de conduta para o ciclo de trabalho sem supervisão humana no teclado.

---

## 1. Regras de Preservação de Escopo
1. O agente é restrito a trabalhar nos arquivos estritamente necessários para o ticket atual.
2. É proibido alterar arquivos não relacionados para "limpeza de código" ou refatorações de oportunidade sem autorização explícita na issue.

## 2. Inviolabilidade da Governança
1. Os arquivos `MISSION.md`, `FACTORY_RULES.md`, `AGENTS.md` e `core/orchestrator/guard.py` estão na lista de caminhos protegidos.
2. Nenhuma alteração neles será aceita por automação.

## 3. Validação Sem Desculpas
1. O agente nunca pode marcar uma tarefa como pronta alegando que "os testes falharam por causa do ambiente". Se o teste falhou, o código não funciona.
2. Todo PR deve passar na validação determinística de marcadores (`core/harness/runner.py`) e na auditoria adversarial cruzada.

## 4. Política de Custos e Modelos
1. Micro-tarefas e gerações intermediárias devem utilizar primeiro o cluster local Ollama (`qwen-fast`, `qwen-deep`).
2. Consultas de contexto extenso usam `gemini-3.8-flash`.
3. Modelos de alto custo (`claude-3.7-sonnet`, `deepseek-r1`) devem ser acionados prioritariamente na fase de arquitetura ou em refatorações cirúrgicas críticas.
