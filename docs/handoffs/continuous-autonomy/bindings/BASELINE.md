# Reconciliação de Baseline Local e Remota (HF-26-03)

Versão 1.0 · 18/09/2026 · Ticket de Arquitetura Superior `HF-26-03` · Handoff Normativo.

---

## 1. Contexto e Inventário de Ancestralidade Git

A baseline de planejamento do pacote de autonomia contínua foi congelada no commit local `83e5298eb231599076811802dceac8575c7f6feb`.
Na data de observação, o ponteiro da branch remota do servidor (`origin/main`) encontrava-se em `00133f6093d0b78090b04deacc61c82dcee5abb7`.

A verificação determinística de ancestralidade (`git merge-base --is-ancestor 00133f6 HEAD`) confirma que `origin/main` é um **ancestral estrito** do histórico local. Entre o servidor remoto e a baseline local de planejamento existiam exatamente **7 commits não integrados remotamente**.

---

## 2. Exame Individualizado dos Sete Commits Herdados

Para evitar a "publicação de herança incidental" (tratar código local como se já estivesse implantado e testado no servidor), cada commit foi analisado quanto ao seu escopo e impacto em dependências downstream:

| Commit SHA | Marco | Título | Área Funcional | Impacto Remoto e Dependências |
| --- | --- | --- | --- | --- |
| `b0c31d0` | HF-22 | feat(knowledge): implement Segundo Cerebro MCP client, fail-closed provenance, and HF-22 handoff | Conhecimento / MCP | Cliente MCP do Segundo Cérebro. Desacoplado do runtime central da fábrica; não bloqueia unidades do core. |
| `1730537` | HF-21 | feat(marketing): implement HF-21 enterprise brand, CMS, CRM, SEO, GA4 and Google Ads engine | Marketing Digital | Motor de marketing de crescimento empresarial. Totalmente isolado do loop de execução técnica autônoma. |
| `3f2a832` | HF-21 | feat(marketing): enforce mandatory Gate G1 human approval before publishing blog posts to production | Governança Marketing | Enforçamento de Gate G1 para publicação de posts. Desacoplado da execução técnica. |
| `8ca2cf1` | HF-23 | feat(portfolio): implement HF-23 portfolio efficiency, capacity slots, budgets, and paid-first routing | Gestão de Portfólio | Gerenciador de capacidade e cotas de orçamento. **Bloqueia exclusivamente** as unidades dependentes: `HF-23-01` e `HF-05-06`. |
| `10aaaf7` | HF-25 | feat(evolution): implement HF-25 factory self-evolution and cross-project reusable catalog | Auto-Evolução | Motor de evolução e catálogo reutilizável. **Bloqueia exclusivamente** a unidade dependente: `HF-25-01`. |
| `d58c06a` | HF-24 | feat(enterprise): implement HF-24 enterprise profile, residency, cryptographic audit chain and SLA | Perfil Enterprise | Trilha de auditoria criptográfica e perfis de soberania de dados. Desacoplado do runtime de desenvolvimento. |
| `83e5298` | HF-23 | feat(portfolio): integrate live activation tests and runner for ATRIUM and JARVIS | Testes de Ativação | Runner de ativação do portfólio. Relevante para a homologação final de fatia vertical (`HF-03-08`). |

---

## 3. Classificação de Compatibilidade das 33 Unidades de Autonomia

Seguindo o oráculo normativo (*"ausência remota de portfolio/evolution bloqueia só dependentes; não publicar herança incidental"*), as 33 unidades do DAG HF-26 são divididas em duas classes estritas:

### 3.1. Unidades Compatíveis Imediatas (27 tickets)
Estas unidades operam exclusivamente sobre os contratos canônicos locais ou dependem de abstrações estáveis, podendo ser desenvolvidas, testadas e integradas localmente sem requerer que o servidor remoto já possua os commits HF-21 a HF-25:
* **Política & Governança**: `HF-26-01`, `HF-26-02`, `HF-26-03`
* **Controle & Store**: `HF-05-02`, `HF-05-03`, `HF-05-04`, `HF-05-05`
* **Intake & Handlers**: `HF-08-01`, `HF-08-02`, `HF-08-03`, `HF-08-04`, `HF-08-05`
* **Executores & Rotas**: `HF-07-01`, `HF-07-02`, `HF-07-03`
* **Ciclo de Desenvolvimento**: `HF-09-01`, `HF-09-02`, `HF-11-01`
* **Release & Dokploy**: `HF-12-01`, `HF-12-02`, `HF-12-03`, `HF-12-04`
* **Memória & Pesquisa**: `HF-10-01`, `HF-10-02`
* **Roadmap & Observador Base**: `HF-13-01`, `HF-13-02`, `HF-15-01`

### 3.2. Unidades de Integração Remota / Condicionais (6 tickets)
Estas unidades dependem diretamente da presença física dos módulos de portfólio ou evolução no ambiente alvo e só devem ser ativadas operacionalmente após a sincronização desses pacotes:
* `HF-23-01` (*Reserva única e justiça de portfólio*): exige o motor de cotas do commit `8ca2cf1`.
* `HF-25-01` (*Evolução avaliada e aplicada após restart*): exige o engine do commit `10aaaf7`.
* `HF-05-06` (*Entrypoints cloud permanentes*): coordena com a reserva única `HF-23-01`.
* `HF-03-07` (*Preflight por host e projeto*): valida manifestos remotos antes da ativação.
* `HF-03-08` (*Ativação isolada e fatia vertical*): ativação real no servidor.
* `HF-15-02` (*Aceitação 24h multi-projeto*): certificação final de ponta a ponta.

---

## 4. Hashes de Contratos Congelados

Os contratos críticos da camada de fluxo e política estão congelados com os seguintes hashes SHA-256:

* `core/workflow/contracts.py`: `13b77111960cbd7d5716e3b93953fb956fa0436630389daa283af676ca9b65db`
* `core/workflow/effective_policy.py`: `5403be7589cb7a39b2e8152414694b04e990f9a178a3a0ff5d33acf55619b774`
* `core/workflow/policy_binding.py`: `884e333451cc961dc7740166c168cb841737163a37fffc33229ffe22d9eecc81`
* `core/orchestrator/delivery.py`: `bd4e63d8f6ed084dfc3d3ad624e7e5680e9145572dde22ef5b4b772f4ef25ea6`
* `core/workflow/readiness.py`: `f6e9bc17a84fd8852a8eee5cd2e63f0a36a55189acf70500c4840e90eac4d038`
* `core/workflow/verification.py`: `2bf281835520a0632ac5a1f74c740d78625cf546af6c63aba951321d1fe52fcc`
* `core/workflow/runtime.py`: `13bdc344e70ba40b7ceb583d667040a4c7a73df24bce80d887f694a0188e4055`

---

## 5. Conclusão do Handoff e Próximos Desbloqueios

A reconciliação do `HF-26-03` conclui o binding de arquitetura superior sobre as origens e ancestralidade do repositório.
Com o cumprimento deste handoff, o DAG de autonomia contínua desbloqueia os seguintes sucessores:
* **`HF-05-02`**: *Binding do controle cloud e ownership* (dependia conjuntamente de `HF-26-02` e `HF-26-03`).
* **`HF-07-01`**: *Binding de executores e contas reais* (dependia de `HF-26-03`).
* **`HF-12-01`**: *Binding de build e targets reais* (dependia de `HF-26-03`).
