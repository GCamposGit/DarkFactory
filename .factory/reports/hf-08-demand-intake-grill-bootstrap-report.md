# HF-08 — Relatório de Entrada de Demandas, Grill, Especificação e Bootstrap Integrado

## Resumo

- **Ticket**: `HF-08`
- **Parent**: `HF-08`
- **Status**: **CONCLUÍDO (100% Validado Deterministicamente)**
- **Escopo**: Entrada multimodal de demandas (texto, JSON, áudio local), motor de Grill orientado a decisões (Cenário G1), resolução autônoma de dependências e compilação de handoff (Cenário G2), bootstrap de projetos greenfield/brownfield (Skill 07) e CLI headless.
- **Ambiente**: Python 3.12, Windows 11, Worktree Local.

---

## 1. Entregas e Modificações

### 1.1. Adaptador de Contratos Normativos de Workflow (`core/demands/contracts_adapter.py`)
- Mapeamento estrito entre `UserTicket` / `DemandInput` e os contratos Pydantic v2 de HF-04:
  - `build_grill_record`: constrói `GrillRecord` auditável (`schema_version=1`), validando que `ready_for_spec=True` proíbe perguntas pendentes e exige critérios de exemplo e decisões materiais respondidas.
  - `build_environment_manifest`: gera `EnvironmentManifest` sanitizado, proibindo DSNs com credenciais, tokens em queries ou fragments e expondo ferramentas, serviços e `worker_identity`.
  - `build_manual_dependency`: formula `ManualDependency` com passos numerados consecutivos, comando de `final_probe` seguro e bloqueio seletivo de estágios (`blocked_stages`).
  - `build_workflow_handoff`: compila `WorkflowHandoff` com tier `PlannerTier.HIGH`, caminhos permitidos e comandos de validação.
  - `create_testing_verification_context`: cria `VerificationContext` e `PlanApproval` para avaliação determinística em portões de teste.

### 1.2. Motor de Grill Integrado & Invariante G1 (`core/demands/grill.py`)
- Implementada a avaliação de ambiguidade `evaluate_ambiguity(ticket)`:
  - **Demanda Clara**: se o problema é detalhado (>20 caracteres sem marcadores TODO/TBD), non-goals delimitados e critérios verificáveis presentes, emite `GrillRecord(ready_for_spec=True)` com zero perguntas artificiais redundantes. O run avança sem interrupção.
  - **Demanda Ambígua**: se há indefinições de escopo ou dependências materiais, formula de 1 a 3 perguntas cirúrgicas com 2 ou 3 opções com recomendação explícita (`is_recommended=True`) e consequência curta. O run é suspenso em `WorkflowState.WAITING_HUMAN`.
  - **Retomada de Jobs Afetados**: ao submeter respostas (ou auto-aceitar recomendações), as decisões são gravadas com `decision_source`, o `GrillRecord` é finalizado e o run no `WorkflowRuntime` retoma para `PLANNING_HIGH`, liberando apenas os jobs afetados.

### 1.3. Resolução Autônoma de Dependências & Invariante G2 (`core/demands/integrated_service.py`)
- Implementado o protocolo sequencial de dependências da Seção 2 de `HYBRID_AUTONOMY_REQUIREMENTS`:
  - **Equivalência Técnica**: dependências com alternativa aceitável e testada (`AlternativeAttempt(tested=True, equivalent=True)`, ex: SQLite local em vez de cluster Redis/Postgres) são resolvidas de forma autônoma sem abrir dependência humana, permitindo aprovação no `ReadinessGate`.
  - **Dependência Insubstituível**: chaves de produção ou autorizações exclusivas do owner geram `ManualDependency` em status `WAITING`, bloqueando `READY_FOR_HANDOFF` ou `DELIVERED` até a realização do probe seguro com receipt aprovado.
  - Emissão de `WorkflowHandoff` validada formalmente pelo `ReadinessGate().evaluate()`.

### 1.4. Bootstrap Autônomo de Projetos Greenfield e Brownfield (Skill 07)
- Integração com `core.adoption`:
  - **Greenfield**: inicializa repositório git baseline (`initialize_project`), adiciona e commita arquivos de governança (`MISSION.md`, `FACTORY_RULES.md`, `AGENTS.md`), gera o lockfile de proveniência (`.factory/darkfac.lock.json`), extrai o `EnvironmentManifest` da stack e instancia o primeiro run no `WorkflowRuntime`.
  - **Brownfield**: inspeciona repositório existente, executa plano de adoção namespaced em `.factory/runtime/`, verifica integridade e registra o run no runtime.
  - Garante que uma conversa de usuário crie backlog, ambiente e critérios sem edição manual de arquivos YAML de fluxo.

### 1.5. Ingestão Multimodal por Áudio (Skill 09)
- Método `receive_audio_demand`: processa arquivos de áudio gravados (reuniões, podcasts, chamadas) via transcrição local faster-whisper ($0), sintetizando o problema, a jornada e os critérios, encaminhando o resultado diretamente para o fluxo de Grill.

### 1.6. Interface CLI Headless e Ergonomia no Windows (`core/demands/cli.py`)
- Novos subcomandos headless suportando saída estruturada via `--json`:
  - `python -m core.demands.cli intake`: submete demanda (texto ou `--audio-file`), avalia Grill e registra run.
  - `python -m core.demands.cli grill`: executa sessão de Q&A interativa ou headless (`--auto-accept`).
  - `python -m core.demands.cli plan`: resolve dependências, compila handoff e emite relatório do gate.
  - `python -m core.demands.cli bootstrap`: inicializa ou adota projetos novos com Skill 07.

---

## 2. Validação Determinística

| Suíte / Comando | Testes Executados | Resultado |
| :--- | :--- | :--- |
| `python -m pytest tests/test_hf08_demand_intake_grill_bootstrap.py -v` | 9 testes focais HF-08 (G1, G2, Intake, Runtime, Bootstrap, Áudio, CLI) | **9 PASSED (100%)** |
| `python -m pytest tests/test_demands.py -v` | 6 testes de backlog e persistência de demandas | **6 PASSED (100%)** |
| `python -m pytest tests/test_criar_mecanismo_de_grill_.py -v` | 7 testes do mecanismo de grill legado | **7 PASSED (100%)** |
| `python -m pytest tests/test_project_adoption.py -v` | 9 testes de adoção de projetos e worktrees | **9 PASSED (100%)** |
| `python -m pytest tests/test_workflow_contracts.py -v` | 24 testes de contratos e validações HF-04 | **24 PASSED (100%)** |
| `python -m pytest tests/test_workflow_runtime.py -v` | 22 testes de runtime e outbox durável HF-05 | **22 PASSED (100%)** |
| `python -m pytest tests/test_skills_modularization.py -v` | 74 testes de paridade de skills HF-06 | **74 PASSED (100%)** |
| `python C:\dev\DarkFac\core\harness\runner.py --quick` | Suíte global do harness oficial DarkFac | **746 PASSED, 2 SKIPPED, [HARNESS_PASS]** |

---

## 3. Próximo Passo

Conforme a Seção 12 do Plano Híbrido, o próximo ticket da trilha crítica é:
**HF-09: Ciclo de implementação, qualidade e revisão independente por perfil.**
