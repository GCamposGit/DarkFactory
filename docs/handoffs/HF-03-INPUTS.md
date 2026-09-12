# Handoff Técnico HF-03: Insumos de Implantação e Infraestrutura Cloud

- **Origem**: HF-02-08 / [ADR-HF-001](../decisions/ADR-HF-001-runtime.md)
- **Destino**: HF-03 (Plano de Instalação, Storage e Worker Cloud Isolado)
- **Status**: Especificado e Pronto para Execução
- **Data**: 2026-09-11
- **Planejador**: Astra (Alta Inteligência)

---

## 1. Banco de Dados e Conectividade Privada

| Parâmetro | Especificação Normativa | Restrições de Segurança |
| :--- | :--- | :--- |
| **Instância de Destino** | PostgreSQL 16+ na VPS Dokploy existente | Isolamento de rede local/Docker; proibido expor porta 5432 na internet pública |
| **Nome do Banco (Lógico)** | `darkfac_hf02_prod` (ou `darkfac_hf02_lab` para homologação) | Prefixo obrigatório `darkfac_hf02_` conforme contrato HF-02-01 |
| **Usuário do Banco** | `darkfac_worker` | Sem privilégios `SUPERUSER`, `CREATEDB` ou `CREATEROLE`; permissões apenas no schema do banco |
| **Injeção de Credenciais** | Variável `DARKFAC_HF02_DATABASE_URL` no container | Proibido serializar DSN em arquivos JSON, commits de Git, logs de stdout/stderr ou tracebacks |
| **Pool de Conexões** | Min: 2, Max: 10 conexões ativas | Compatível com o envelope de memória de 4 GB RAM da VPS |
| **TLS / Certificados** | SSL `require` ou `verify-full` | Criptografia em trânsito obrigatória para tráfego entre nós |

---

## 2. Topologia de Processos, Coordenador e Workers

1. **Coordenador Cloud (Dokploy / Docker)**:
   - Serviço em container único gerenciado por política de reinício (`restart: unless-stopped`).
   - Responsável por ingestão de demandas, despacho de filas duráveis e escuta de eventos externos.
   - Recursos alocados: máximo de 512 MiB RSS em repouso, pico de até 1.5 GiB durante concorrência máxima.
2. **Worker Cloud Isolado**:
   - Processos trabalhadores desacoplados para execução de etapas de compilação, testes e geração de artefatos.
   - Comunicação com o coordenador mediada estritamente pelo banco PostgreSQL compartilhado e filas DBOS.
   - Isolamento de sistema de arquivos via volumes Docker descartáveis (`tmpfs` ou volumes mapeados por run).
3. **Slots, Locks e Pools Independentes**:
   - Concorrência máxima inicial: **2 slots simultâneos** para preservar a estabilidade da VPS.
   - Locks transacionais gerenciados pelo DBOS sobre tabelas de sistema PostgreSQL (`dbos_workflow_status`, `dbos_workflow_events`).
   - Workers operam com fencing tokens garantindo que apenas o processo com a posse válida do lease possa emitir checkpoints.

---

## 3. Versão Congelada, Dependências e Estratégia de Upgrade

- **Pacote Core Durável**: `dbos == 2.31.1` (conforme [requirements.lock.txt](../../spikes/runtime_choice/requirements.lock.txt)).
- **Driver de Banco**: `psycopg[binary] >= 3.2.0, < 3.3.0`.
- **Monitor de Sistema**: `psutil >= 6.0.0`.
- **Política de Upgrade**:
  - Upgrades de versão menor do DBOS exigem validação prévia na suite de testes do spike (`spikes/runtime_choice/tests/`).
  - O versionamento de workflows é gerenciado declarativamente por `application_version='v1'` / `'v2'`.
  - Novos deploys mantêm os workers da versão anterior ativos até que todos os workflows em voo atinjam estado terminal (`completed` ou `failed`), evitando interrupções forçadas.

---

## 4. Protocolo de Transição e Convivência com SQLite

1. **Início de Novos Workflows**:
   - Todo novo workflow despachado na nuvem usará exclusivamente o backend DBOS PostgreSQL.
2. **Conclusão de Workflows Antigos**:
   - Workflows iniciados localmente sob Native SQLite (`.factory/orchestrator.sqlite3`) concluem no runtime local.
   - **Proibição de Conversão Direta**: Não há conversão direta de checkpoints SQLite para tabelas internas do DBOS. As estruturas de dados são independentes.
3. **Auditoria Unificada**:
   - O DarkHub (HF-13) lerá os metadados de execução através do contrato de abstração `RunSummary`, sem acoplamento com o driver de banco subjacente.

---

## 5. Armazenamento de Artefatos e Isolamento de Estado

- **Artefatos Volumosos**:
  - Relatórios, builds, dumps de testes e áudios não devem ser serializados como blobs no banco de dados.
  - Devem ser armazenados no sistema de arquivos sob `.factory/artifacts/<workflow_id>/` ou transferidos para bucket compatível com S3 (Cloudflare R2) no HF-12.
- **Evidências de Step**:
  - Cada step grava no DBOS apenas o hash SHA-256 do artefato produzido e referências sanitizadas (`artifact_refs`), prevenindo bloat no PostgreSQL.

---

## 6. Gatilhos Automáticos, Reconciliação e Resiliência

1. **Reconciliação Pós-Reboot (Crash Recovery)**:
   - Ao iniciar o container do coordenador, o DBOS varre workflows com status `PENDING` ou `RUNNING` não atualizados dentro do período de lease e retoma sua execução a partir do último checkpoint confirmado.
2. **Gatilhos de Espera Durável**:
   - Steps em espera humana (`DBOS.recv`) persistem a subscrição de tópicos sem consumir CPU.
   - O recebimento de callbacks (Telegram Bot ou DarkHub API) despacha mensagens idempotentes com a chave `workflow_id + step_id + choice`.
3. **Evolução Diária de Modelos**:
   - O roteador de modelos (Skill 03 / `core/router/`) consulta o ledger diário de eficiência de Pareto gerado pela Skill 12 (`daily-model-benchmark`), ajustando dinamicamente os despachos sem necessidade de rebuild do container.

---

## 7. Envelope Orçamentário e Capacidade

- **Licenças de Software**: US$ 0 (100% stack open-source permissiva: Python, DBOS Apache 2.0, PostgreSQL, Dokploy).
- **Infraestrutura Cloud**: Reserva de **US$ 15–35/mês** para a VPS com 4 GB RAM e volume de persistência.
- **Consumo de IA**: Alocação de cotas por workflow gerenciada pelo monitor de uso (DF-10 / DF-12). Ausência de saldo em provedor de fronteira aciona automaticamente fallback local (Ollama) ou bloqueio limpo com aviso no Telegram.

---

## 8. Tarefas Manuais Exclusivas do Owner (com Probes de Retomada)

Conforme a regra E4 dos requisitos de autonomia, as seguintes dependências humanas estão delimitadas:

### Dependência 1: Provisionamento do Banco Descartável na VPS
- **Ação do Owner**:
  1. Acessar o painel Dokploy / PostgreSQL existente na VPS.
  2. Criar o banco de dados `darkfac_hf02_prod`.
  3. Criar o usuário `darkfac_worker` com senha segura e permissões completas apenas neste banco.
  4. Configurar a variável de ambiente `DARKFAC_HF02_DATABASE_URL` no Dokploy.
- **Probe Automatizado de Retomada**:
  ```powershell
  python C:\dev\DarkFac\spikes\runtime_choice\cli.py preflight
  ```
  *Critério de Sucesso*: O preflight reportará `"dbos_postgres": {"database_connection": "ready", "status": "ready"}`.

### Dependência 2: Autorização de Firewall de Saída / Rede Privada
- **Ação do Owner**:
  - Assegurar que os containers Dokploy possam comunicar-se na rede interna `dokploy-network`.
- **Probe Automatizado de Retomada**:
  - Teste de ping/handshake TCP entre containers via script de bootstrap do HF-03.

---

## 9. Contratos a Serem Detalhados em HF-04 e HF-05

- **HF-04**: Especificará os contratos rigorosos de etapas (`StepContract`), o modelo de contexto imutável (`WorkflowVerificationContext`), a máquina de estados determinística e as regras do Grill.
- **HF-05**: Integrará a engine DBOS aos adaptadores de runtime, conectando o outbox durável, o tratamento de eventos e a governança orçamentária.
