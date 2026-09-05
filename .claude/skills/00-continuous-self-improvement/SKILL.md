---
name: continuous-self-improvement
description: Ciclo contínuo e prioritário de auto-aperfeiçoamento e calibração por sessão. Dispara obrigatoriamente a cada segundo prompt do usuário para avaliar a demanda, inferir preferências profundas e atualizar a skill apropriada para garantir one-shot no próximo pedido. Executa Root Cause Analysis (RCA) determinístico para cada erro e falha pregressa, sintetizando patches para eliminar reincidência e extrapolando aprendizados para casos análogos em todo o ecossistema.
---

# 00 - Continuous Self-Improvement & SOTA Recursive Learning Loop

O **Continuous Self-Improvement Engine** é o loop mestre de auto-evolução em tempo real da DarkFac.
Fundamentado na literatura de fronteira de **Self-Evolving Agents (2025–2026)** — integrando os paradigmas de **SICA (Self-Improving Coding Agent)**, **Gödel Agent / Darwin Gödel Machine (DGM)**, **Dual-Process Architecture (DPA System 1/2)**, **ExpeL (Experiential Learning)** e **Voyager (Executable Skill Synthesis)** — este sistema transforma a DarkFac em um agente recursivo com aprendizado contínuo empiricamente comprovado.

---

## 🏛️ Arquitetura Dual-Process (DPA)

```text
               ┌──────────────────────────────────────────────────────────┐
               │          SISTEMA 1: EXECUÇÃO RÁPIDA ONE-SHOT            │
               │  - Primes context via get_active_system1_context()      │
               │  - Injeta heurísticas de alta confiança e regras RCA     │
               │  - Zero perguntas triviais, execução cirúrgica direta   │
               └────────────────────────────┬─────────────────────────────┘
                                            │
                                  [Turno do Usuário]
                                            │
               ┌────────────────────────────▼─────────────────────────────┐
               │         SISTEMA 2: REFLEXÃO METACONSTITUTIVA             │
               │          (Cadência: a cada 2º prompt ou follow-up)        │
               │                                                          │
               │  1. Análise Contrastiva de Trajetórias (ExpeL / SICA)    │
               │     Compara tentativa inicial vs correção do usuário     │
               │  2. Root Cause Analysis 5-Whys Determinístico            │
               │  3. Code Judge Verification Gate (Inviolabilidade)       │
               │     Patch só é aceito se passar em teste executável      │
               │  4. Extrapolação Analógica Cruzada para Módulos Irmãos   │
               │  5. Poda Anti-Entropia de Policy Debt (Anti-Bloat)       │
               └──────────────────────────────────────────────────────────┘
```

---

## 🎯 Os 5 Pilares do Estado da Arte

### 1. The Universal Every-Prompt Checkpoint Cadence (Turno 1 em diante)
- O auto-aperfeiçoamento dispara **obrigatoriamente em todas as sessões, desde o primeiro prompt (Turno 1 em diante)** e diante de qualquer follow-up corretivo:
  - Trata o primeiro prompt como potencial continuação ou correção acumulada de sessões pregressas.
  - O agente para imediatamente para diagnóstico e alinhamento de intenção.
  - Registra a preferência e sintetiza a regra na skill alvo para garantir que o usuário **nunca precise pedir a mesma correção duas vezes**.

### 2. Análise Contrastiva de Trajetórias (Contrastive Trajectories)
- Em vez de apenas registrar texto livre, o sistema compara $T_{inicial}$ com $T_{corrigido}$.
- Extrai o **Key Delta** (ex.: "Verbosidade -> Tabela compacta", "Código inline -> Script headless testado") e converte o delta em preferência canônica.

### 3. Code Judge Determinístico (Verification-Grounded Self-Patching)
- Nenhuma alteração de skill, regra ou correção de código é promovida com base em "garantias conversacionais" de LLM.
- O **Code Judge** executa comandos determinísticos (`verify_patch_with_code_judge`). Se o teste falhar ou o exit code for $\neq 0$, o patch é sumariamente bloqueado.

### 4. Extrapolação Analógica Cruzada (Cross-Domain Transfer)
- Um aprendizado extraído em um domínio nunca fica isolado.
- Exemplo: Um tratamento de encoding UTF-8 no Windows aprendido em `core.audio` é automaticamente projetado para `core.benchmarks`, `core.research` e `hub.backend`.

### 5. Poda Anti-Entropia (Policy Debt Pruning)
- O acúmulo desordenado de regras gera alucinações e degradação de atenção (*policy debt*).
- O Sistema 2 consolida regras redundantes, mescla reforços e desativa heurísticas obsoletas de forma automatizada.

---

## 🛠️ Operação Headless do Motor (`core/learning/cli.py`)

### 1. Injetar Priming do Sistema 1 (Contexto de Alta Precisão)
```bash
# Obtém as convenções e regras de ouro ativas para o domínio atual
python -m core.learning.cli prime --domain core.benchmarks
```

### 2. Registrar Turno e Avaliar Checkpoint
```bash
python -m core.learning.cli record-turn \
  --prompt "Ajuste o timeout do runner para 120s" \
  --intent "Prevenir timeout no pytest sob carga pesada no Windows" \
  --followup \
  --skill "autonomous-piv-loop" \
  --gap "Timeout de 30s insuficiente em hardware com I/O lento"
```

### 3. Registrar Root Cause Analysis (RCA) com Code Judge
```bash
python -m core.learning.cli rca \
  --category timeout \
  --symptom "Runner travou aos 30s durante teste de áudio" \
  --mechanism "Subprocesso sem timeout explícito no Windows PowerShell" \
  --root-cause "Falta de parâmetro timeout=120 e bloco TimeoutExpired" \
  --patch "Adicionado timeout padronizado em todas as invocações de runner" \
  --rule "Todo subprocess.run deve declarar timeout explícito e tratamento de exceção" \
  --test-file "tests/test_learning_engine.py"
```

### 4. Executar Poda Anti-Entropia de Policy Debt
```bash
# Consolida regras repetidas e desativa duplicatas
python -m core.learning.cli prune
```

### 5. Rodar o Benchmark Empírico de Aprendizado
```bash
# Executa a suíte de testes que afere a eficácia real do loop
python -m core.learning.cli benchmark
# ou diretamente via módulo:
python -m core.learning.benchmark
```

---

## 📊 A Suíte de Benchmarking Empírico

O benchmark (`core/learning/benchmark.py`) avalia deterministicamente 4 dinâmicas essenciais:

| Cenário | O que Mede | Critério de Sucesso |
| :--- | :--- | :--- |
| **1. Recurring Error Extinction** | Erro injetado uma vez nunca mais se repete após RCA | Taxa de extinção = **100%** |
| **2. One-Shot Convergence** | Follow-up captura preferência e tarefa seguinte executa em 1 turno | Taxa de One-Shot = **100%** |
| **3. Cross-Domain Transfer** | Regra aprendida em um módulo é projetada para módulos irmãos | Cobertura de domínio = **100%** |
| **4. Policy Debt Pruning** | Eliminação de duplicatas e unificação de reforço sem conflitos | Coerência limpa = **100%** |

---

## 🏛️ Governança Inviolável

1. **Inviolabilidade de Testes de Regressão**: Toda falha corrigida adiciona um caso permanente no `tests/`. É proibido desativar ou deletar testes de regressão anteriores.
2. **Sincronização Multi-Ambiente**: Toda evolução em `.agents/skills/` é espelhada instantaneamente para `.claude/skills/` via `python scripts/sync_skills.py`.
3. **Persistência Centralizada**: O estado cumulativo é mantido em `.factory/learning/learning_ledger.json`.
