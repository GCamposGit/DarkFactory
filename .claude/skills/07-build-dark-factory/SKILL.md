---
name: build-dark-factory
description: Instala a infraestrutura completa de uma Fábrica de Software Autônoma (Dark Factory Nível 3+) em qualquer repositório (greenfield ou brownfield). Configura a camada de orientação (MISSION.md, FACTORY_RULES.md), o harness de validação, a máquina de estados, o guardrail de arquivos protegidos e o agendador autônomo. Use quando o usuário quiser transformar um projeto em um repositório autônomo auto-gerenciável.
---

# Build Dark Factory: Engenharia de Software Autônoma (Nível 3+)

Esta skill transforma uma base de código em uma **Dark Factory**: um repositório onde especificações entram como issues e saem como código validado e mesclado sem ninguém no teclado.

## O Dial de Autonomia (The Autonomy Dial)

| Nível | O que é Automático | O que o Humano Faz |
| :--- | :--- | :--- |
| **0** | Workflows e scripts existem | Executa tudo manualmente |
| **1** | Issue rotulada -> PR abre | Revisa diff e faz merge manual |
| **2** | Validador roda e emite veredito | Faz merge manual |
| **3 (Padrão)** | **Auto-merge quando todos os portões e revisões forem verdes** | Escreve issues/PRD e faz releases |
| **4** | Sistema faz triagem e gera seus próprios testes de estresse | Escreve issues de alto nível |
| **5** | Sistema cria as próprias issues a partir da MISSION | Apenas monitora resultados |

> **Meta do Projeto**: Construir diretamente para o **Nível 3**. Níveis inferiores são etapas transitórias de calibração.

## Ordem de Construção dos 5 Componentes

```text
[0. Entrada] PRD formal com lista explícita de non-goals
      │
      ▼
[1. Guidance Layer] MISSION.md, FACTORY_RULES.md, AGENTS.md (Barato e de alto impacto)
      │
      ▼
[1.5. Walking Skeleton] Fatia mínima vertical funcional (Apenas em greenfield)
      │
      ▼
[2. Validation Harness] Runner com marcadores, E2E headless e portões determinísticos
      │
      ▼
[3. Workflow Engine] Máquina de estados (state.py), Guardrail (guard.py), PIV Loop
      │
      ▼
[4. Deployment & CI] Pipeline de entrega contínua
      │
      ▼
[5. Trigger / Agendador] Ativação do polling automático (Task Scheduler / Cron)
```

## Passo a Passo de Implantação

### 1. Criar a Camada de Orientação (Guidance Layer)
Copie os templates de governança para a raiz do repositório:
- `MISSION.md`: Objetivo central e lista rígida de *out-of-scope*.
- `FACTORY_RULES.md`: Regras de conduta autônoma e limites de gastos.
- `AGENTS.md`: Padrões de código do projeto.

### 2. Configurar o Validation Harness
Instale o runner em `core/harness/runner.py` e configure o `harness.config.json` para mapear os comandos de teste da sua aplicação.

### 3. Ativar o Guardrail Determinístico
Garanta que nenhum agente consiga alterar a governança executando:
```bash
python core/orchestrator/guard.py HEAD
```

### 4. Demonstrar a Primeira Volta Completa (The First Lap)
Antes de ligar o agendador automático, execute manualmente um ciclo completo com uma issue simples:
1. Issue cadastrada em `.factory/state.json`.
2. Planejamento via `02-plan-product-architecture`.
3. Implementação via `04-autonomous-piv-loop`.
4. Validação via `05-validation-harness`.
5. Auditoria via `06-adversarial-review`.
6. Auto-merge realizado com sucesso.

---

## 🧠 Continuous Self-Improvement & Failure RCA Integration

1. **Instalação do Loop Mestre de Aprendizado (`00-continuous-self-improvement`)**:
   - Toda Dark Factory deve incluir o diretório `.factory/learning/` e a biblioteca `core/learning/` na sua fundação.
   - O agendador e o orquestrador acionam o `ContinuousLearningTracker` para auditar a taxa de execução One-Shot e alimentar melhorias cumulativas.
2. **RCA de Falhas de Implantação e Transição de Estados**:
   - Se uma issue travar no estado `NEEDS_FIX` por mais de 2 voltas, o sistema dispara RCA automático para diagnosticar a causa sistêmica (especificação vaga, dependência quebrada ou teste frágil).
   - O patch é aplicado diretamente na camada de orientação ou no harness antes de retomar a execução autônoma.

