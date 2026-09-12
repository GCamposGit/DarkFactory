---
name: meta-skills-evolver
description: Analisa o histórico de execuções da fábrica autônoma, audita drift de regras, gasto de tokens e falhas recorrentes para sintetizar novas skills, calibrar a matriz de roteamento de modelos e atualizar o repositório. Use periodicamente para auto-aperfeiçoamento do ecossistema de agentes.
---

# 08 - Meta-Skills Evolver: Auto-Evolução do Ecossistema

Esta skill analisa o histórico operacional de longo prazo da fábrica autônoma, aprendendo com incidentes recorrentes, gargalos de validação e desvios de escopo para sintetizar novas capacidades e otimizar as instruções existentes.

---

## 1. Contratos Normativos da Etapa

### Inputs (Entradas)
- **Histórico de Tarefas**: `.factory/state.json` e relatórios em `.factory/reports/`, focando em tarefas que transicionaram por `NEEDS_REPLAN` ou `FAILED_VALIDATION`.
- **Knowledge & Learning Ledger**: `.factory/learning/learning_ledger.json` consolidando registros de RCA, preferências confirmadas e contraexemplos.
- **Métricas de Inferência e Telemetria**: Consumo de tokens, latência e custo por modelo provenientes do `model-router` e benchmarks diários.

### Ações e Procedimento Executável
1. **Auditoria de Desvio de Regras (Rules Drift)**:
   - Compare o comportamento empírico dos agentes com os padrões técnicos do `AGENTS.md` e regras de arquitetura.
   - Identifique atalhos informais que violam contratos normativos.
2. **Síntese de Novas Skills**:
   - Quando um procedimento operacional ou sequência de subprocessos for repetido 3 ou mais vezes de forma ad-hoc por agentes, sintetize um novo pacote padronizado em `.agents/skills/<nova-skill>/SKILL.md`.
3. **Auditoria de Custo e Calibração de Modelos**:
   - Analise se tarefas médias estão falhando frequentemente com executores econômicos; recalibre a matriz de despacho para balancear custo e Pass@1.
4. **Higienização e Poda de Contexto (Context Ablation)**:
   - Remova instruções obsoletas ou regras duplicadas que incham o contexto sem fornecer garantias determinísticas adicionais.

### Outputs Estruturados
- **Relatório de Evolução**: `.factory/evolution_report.md` com diagnósticos sistêmicos e propostas de ajuste.
- **Rascunhos de Novas Skills**: Novos arquivos de especificação com frontmatter e contratos normativos.
- **Calibração de Roteamento**: Recomendações de pesos e thresholds para o `model_router.py`.

### Portões, Política e Validação
- **Disparo Orientado a Eventos e Marcos**: O evolver é acionado por eventos do scheduler, incidentes sistêmicos ou marcos consolidados de release, **sem depender de contagem de prompts humanos**.
- **Arquivos Protegidos Invioláveis**: O evolver é estritamente proibido de alterar de forma autônoma `MISSION.md`, `FACTORY_RULES.md` e `FACTORY_GOVERNANCE.md`.
- **Validação Pré-Merge**: Qualquer alteração de skill gerada pelo evolver deve ser validada pelo `validation-harness` e espelhada via `scripts/sync_skills.py`.

---

## 2. Sinergia com o Loop de Aprendizado (`00-continuous-self-improvement`)

- Enquanto a Skill 00 atua em nível de micro-iteração imediata (ao término do ticket ou diante de incidente pontual), o `08-meta-skills-evolver` atua em nível macro-estrutural.
- O evolver consome as preferências consolidadas no ledger e as transforma em atualizações definitivas de arquitetura e documentação.
