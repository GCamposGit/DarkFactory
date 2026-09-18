# Binding de Build e Targets Reais de Release (HF-12-01)

Versão 1.0 · 18/09/2026 · Ticket de Arquitetura `HF-12-01` · Parent `HF-12` · Prioridade P0.  
Handoff Normativo de Infraestrutura e Release da Dark Factory.  
Contratos canônicos associados: [CONTRACTS.md](../CONTRACTS.md) | [HF-12-01.md](../HF-12-01.md) | [BASELINE.md](BASELINE.md).

---

## 1. Visão Geral e Filosofia Operacional

O objetivo deste binding de arquitetura superior é formalizar os contratos, adapters, comandos de compilação/build, métodos de captura de digest imutável, endpoints sanitizados e salvaguardas de rollback para todos os alvos de deploy gerenciados pela Dark Factory.

### Princípios Inegociáveis de Release

1. **"Merge ou Deploy não é prova de operação"**:
   A emissão de um evento de merge no Git ou o retorno de um HTTP 200 na chamada de webhook do orquestrador de deploy **NÃO** constituem prova de entrega bem-sucedida. Uma release só é declarada entregue (`DELIVERED`) após a conclusão bem-sucedida de uma jornada sintética operacional de ponta a ponta executada no ambiente destino.
2. **Zero Deploy sem OCI / Bytes Digest Confirmado**:
   Nenhum deploy pode ser despachado com tags mutáveis (como `:latest`) desprovidas de digest imutável (ex: `sha256:...`) ou sobre diretórios estáticos sem conferência rigorosa de árvore de bytes. Tentativas de deploy com digest ausente ou divergente do aprovado são rejeitadas de forma `fail-closed`.
3. **Webhook HTTP 200 sem `external_operation_id` é Inválido**:
   Respostas de API ou webhooks que apenas acusam recebimento sem fornecer um identificador determinístico de operação (`external_operation_id`) para monitoramento de término não satisfazem o contrato de release.
4. **Sanitização Estrita de Credenciais (`SecretReference`)**:
   É terminantemente vedada a persistência ou trânsito de tokens, chaves de API, senhas ou DSNs em texto plano em manifestos, argumentos, logs ou estruturas de dados de contexto. Todas as credenciais utilizam o contrato `SecretReference(provider, locator, variable_name)` e são resolvidas unicamente na fronteira imediata de I/O.
5. **Garantias do Cenário G8 (Projetos Comerciais Pagantes)**:
   Projetos de clientes comerciais pagantes exigem recibo formal de aceite (`ClientAcceptanceReceipt`) antes do deploy em produção. Em caso de falha em produção, o rollback automático para o `last_known_good_digest` é disparado imediatamente, isolando a falha de modo a nunca bloquear outros projetos independentes da fábrica.

---

## 2. Protocolo de Release Autônoma em 5 Etapas

O ciclo de vida de release autônoma para qualquer projeto gerenciado obedece estritamente ao seguinte fluxo determinístico:

```
+-----------------------------------------------------------------------------------+
|                           PROTOCOLO DE RELEASE AUTÔNOMA                           |
+-----------------------------------------------------------------------------------+
| 1. Build Real & Captura Digest  --> Rejeita deploy se digest não confirmado       |
| 2. Disparo de Deploy via API   --> DokployDeployClient / captura operation_id    |
| 3. Monitoramento Determinístico --> Polling de status até 'succeeded' ou 'failed' |
| 4. Jornada Sintética (Smoke)   --> Provas operacionais em produção / staging      |
| 5. Rollback Automatizado       --> Reversão imediata em caso de falha na jornada  |
+-----------------------------------------------------------------------------------+
```

### Etapa 1: Build Real e Captura de Bytes/OCI Digest
* **Ação**: Executa o comando de build específico do alvo dentro de uma worktree isolada e limpa.
* **Captura**: 
  - Containers: extrai o digest OCI da imagem gerada via `docker inspect --format='{{index .RepoDigests 0}}' <image>`.
  - Artefatos estáticos / código: gera o hash SHA-256 recursivo dos bytes do diretório de saída (`dist/`) ou do manifesto canônico do código-fonte.
* **Garantia**: Emite `BuildArtifact`. Se o digest for nulo, vazio ou inconsistente com a baseline aprovada, o pipeline interrompe a execução e emite falha estruturada.

### Etapa 2: Disparo de Deploy com Obtenção de `external_operation_id`
* **Ação**: Aciona o adapter de infraestrutura correspondente (ex: `DokployDeployClient` ou subprocesso local autenticado).
* **Parâmetros**: Resolve referências de segredo exclusivamente na fronteira de rede (ex: `DOKPLOY_API_URL`, `DOKPLOY_API_KEY`).
* **Rastreabilidade**: Registra no `ControlStore` uma entidade `ExternalOperation(operation_key, request_digest, provider, external_id, status='sent', observed_at)`.

### Etapa 3: Monitoramento de Operação até Término Determinístico
* **Ação**: Executa polling com backoff exponencial limitado (ex: intervalos de 3 a 5 segundos, teto de 15 minutos).
* **Estados**: Transição estrita de `sent` para `succeeded` ou `failed`. Respostas ambíguas ou timeouts devolvem `unknown` e suspendem o avanço para a jornada, acionando verificação de recuperação.
* **Proibição**: O orquestrador nunca assume sucesso sem a confirmação de encerramento da compilação e deploy pela infraestrutura.

### Etapa 4: Execução de Jornada Sintética do Usuário Final (Smoke Test)
* **Ação**: Executa cenários de verificação operacional real (read/write roundtrip, checagem de integridade de assets, renderização de templates, probes de conectividade com banco de dados).
* **Métrica**: Mede latência fim a fim em milissegundos.
* **Evidência**: Gera `JourneySmokeTest(passed=True, latency_ms)`. Falha funcional ou latência degradada além do limiar causa reprovação imediata da etapa.

### Etapa 5: Protocolo de Rollback Automatizado
* **Gatilho**: Falha de pré-voo em produção, erro de deploy (Etapa 3) ou falha de jornada sintética (Etapa 4).
* **Ação**: Recupera o identificador do `last_known_good_digest` registrado no histórico do projeto e aciona o comando de redeploy da versão estável anterior sem recompilação.
* **Auditoria**: Emite `RollbackReceipt` vinculando o digest defeituoso, o digest restaurado, o motivo da recusa e o timestamp UTC. Preserva intactos os logs de diagnóstico e o ledger de governança.

---

## 3. Mapeamento Canônico de Targets por Projeto

Abaixo estão fixados os parâmetros operacionais, comandos, credenciais sanitizadas e oráculos de jornada para os 4 projetos registrados na Dark Factory:

### 3.1. Projeto `darkfac` (Dark Factory Core & DarkHub Cloud)

* **Classificação**: Core Cloud PaaS (Container Docker Compose).
* **Host Alvo**: VPS Hetzner CX23 (`darkfac-vps-primary`, IP: `178.105.73.168`, Tailscale: `100.83.176.60`).
* **Domínios**: PaaS `dokploy.ggcampos.com` | Serviço `darkhub.ggcampos.com`.
* **Comando de Build**:
  ```bash
  docker compose -f deploy/dokploy/docker-compose.cloud.yml build
  ```
* **Captura de Digest OCI**:
  ```bash
  docker inspect --format='{{index .RepoDigests 0}}' darkfac-cloud:latest
  ```
* **Secret References Sanitizadas**:
  - `DOKPLOY_API_URL`: Locator `DOKPLOY_API_URL` (URL base da API Dokploy).
  - `DOKPLOY_API_KEY`: Locator `DOKPLOY_API_KEY` (Token de automação PaaS).
  - `DOKPLOY_DEPLOY_URL`: Locator `DOKPLOY_DEPLOY_URL` (Webhook de acionamento).
  - `DARKFAC_HF02_DATABASE_URL`: Locator `DARKFAC_HF02_DATABASE_URL` (PostgreSQL DSN).
  - `GITHUB_PAT`: Locator `GITHUB_PAT` (Token para clone autenticado do repositório privado durante o build do Dockerfile).
* **Adapter de Deploy**:
  `DokployComposeDeployAdapter` via `DokployDeployClient`, despachando para endpoint tRPC `compose.deploy` e consultando `compose.getDeploymentStatus`.
* **Healthcheck**:
  - Interno: `http://127.0.0.1:8001/health`
  - Público: `https://darkhub.ggcampos.com/health` (HTTP 200 esperado).
* **Jornada Sintética Pós-Deploy**:
  - `coordinator_health_probe`: HTTP 200 e confirmação de processo coordinator ativo.
  - `dbos_database_connectivity`: Teste de query no PostgreSQL do ControlStore.
  - `worker_execution_roundtrip`: Enfileiramento e consumo de microtarefa de ping pelo worker.
  - `metrics_telemetry_emit`: Emissão e recepção de telemetria sem erro.
* **Salvaguarda de Rollback**:
  Reversão imediata para o digest OCI estável anterior via comando:
  ```bash
  docker compose -f deploy/dokploy/docker-compose.cloud.yml up -d --no-build
  ```
  Preserva volumes persistentes (`darkfac-artifacts`, `darkfac-test-logs`, `darkhub-factory-data`).

---

### 3.2. Projeto `site-ggcampos` (Portfólio Executivo ATRIUM)

* **Classificação**: Frontend Estático Jamstack (Astro) com CDN / Dokploy Webhook.
* **Host Alvo**: `ggcampos.com` (Dokploy Webhook / CDN Hostinger).
* **Caminho Canônico**: `C:\dev\Site_ggcampos`.
* **Comando de Build**:
  ```bash
  npm run build
  ```
* **Captura de Digest do Artefato**:
  Cálculo de hash SHA-256 recursivo da árvore de arquivos gerada em `dist/` com verificação de não-vacuidade.
* **Secret References Sanitizadas**:
  - `DOKPLOY_STATIC_WEBHOOK_URL`: Locator `DOKPLOY_STATIC_WEBHOOK_URL` (Webhook Dokploy de deploy estático).
  - `HOSTINGER_FTP_HOST`: Locator `HOSTINGER_FTP_HOST` (Fallback de publicação FTP).
  - `HOSTINGER_FTP_USER`: Locator `HOSTINGER_FTP_USER` (Credencial de publicação FTP).
  - `HOSTINGER_FTP_PASSWORD`: Locator `HOSTINGER_FTP_PASSWORD` (Senha cifrada de publicação).
* **Adapter de Deploy**:
  `StaticWebDeployAdapter` acionado via webhook Dokploy com extração de `deployment_id`.
* **Healthcheck**:
  Endpoint `https://ggcampos.com` (HTTP 200 esperado).
* **Jornada Sintética Pós-Deploy**:
  - `homepage_http_200`: Checagem de disponibilidade da página inicial com matching de conteúdo chave.
  - `asset_integrity_audit`: Varredura em todos os scripts JS e folhas CSS linkadas no `<head>` para garantir ausência total de erros 404.
  - `seo_and_opengraph_validation`: Verificação determinística das meta tags canônicas, OG tags e Twitter Cards (Skill 15 / HF-21).
  - `astro_thinking_collection_route`: Validação de rota de artigos `/thinking`.
* **Salvaguarda de Rollback**:
  Restauração atômica do snapshot anterior de `dist/` do repositório de backups mantendo RTO < 60s.

---

### 3.3. Projeto `segundo-cerebro` (Base de Conhecimento Multimodal & MCP)

* **Classificação**: Serviço Local / Daemon MCP (Model Context Protocol).
* **Host Alvo**: Estação de Trabalho Local Windows.
* **Caminho Canônico**: `C:\dev\SegundoCerebro`.
* **Python Executável**: `C:\dev\SegundoCerebro\.venv\Scripts\python.exe`.
* **Comando de Build / Instalação**:
  ```powershell
  C:\dev\SegundoCerebro\.venv\Scripts\python.exe -m pip install -e . --no-deps
  ```
* **Captura de Digest do Artefato**:
  Combinação do Git HEAD SHA verificado e hash criptográfico do schema SQLite / índices Chroma.
* **Secret References Sanitizadas**:
  - `OPENAI_API_KEY`: Locator `OPENAI_API_KEY` (Embeddings neurais de ingestão).
  - `SEGUNDO_CEREBRO_STORAGE_KEY`: Locator `SEGUNDO_CEREBRO_STORAGE_KEY` (Chave de criptografia de índices).
* **Adapter de Deploy**:
  `LocalMCPServiceAdapter` através de `SegundoCerebroClient`.
* **Healthcheck & Probes**:
  - Execução de handshake no protocolo MCP (`stdio://python -m segundocerebro.mcp.server`).
  - Verificação da lista de ferramentas registradas: `search`, `read_note`, `neighbors`, `list_folder`, `outline`, `get_document`, `pack_folder`.
* **Verificação de Integridade de Armazenamento**:
  - SQLite: `PRAGMA integrity_check;` deve retornar estritamente `ok`.
  - Chroma: Verificação de existência da coleção e dimensionalidade vetorial de 1536 dimensões.
* **Jornada Sintética Pós-Deploy**:
  - `mcp_client_health_available`: `SegundoCerebroClient().check_health().available == True`.
  - `semantic_retrieval_query`: Consulta semântica de teste com checagem de retorno status `FOUND` e score de relevância calibrado.
  - `rag_provenance_audit`: Validação do hash SHA-256 do chunk citado contra o texto bruto original no disco (garantia anti-alucinação).
* **Salvaguarda de Rollback**:
  Reversão via `git checkout` para o commit anterior e restauração imediata da cópia de segurança do banco SQLite a partir de `.factory/backups/`.

---

### 3.4. Projeto `jarvis` (Assistente Executivo Inteligente)

* **Classificação**: Daemon Local com Port Healthcheck & Áudio Neural.
* **Host Alvo**: Estação de Trabalho Local Windows.
* **Caminho Canônico**: `C:\dev\Jarvis`.
* **Porta de Serviço**: `8088`.
* **Python Executável**: `C:\dev\Jarvis\.venv\Scripts\python.exe`.
* **Comando de Build / Instalação**:
  ```powershell
  C:\dev\Jarvis\.venv\Scripts\python.exe -m pip install -e .
  ```
* **Captura de Digest do Artefato**:
  Git HEAD SHA combinado com hash do manifesto de configuração local do serviço.
* **Secret References Sanitizadas**:
  - `OPENROUTER_API_KEY`: Locator `OPENROUTER_API_KEY` (Fallback cognitivo).
  - `ELEVENLABS_API_KEY`: Locator `ELEVENLABS_API_KEY` (Voz neural).
  - `JARVIS_DAEMON_TOKEN`: Locator `JARVIS_DAEMON_TOKEN` (Autenticação da API local do daemon).
* **Adapter de Deploy**:
  `LocalDaemonServiceAdapter` gerenciando o ciclo de vida do processo/serviço local.
* **Healthcheck**:
  - Probe de processo: PID ativo e respondendo no Windows Task Manager / PowerShell.
  - Probe HTTP: `GET http://127.0.0.1:8088/health` retornando HTTP 200 e status `online`.
* **Jornada Sintética Pós-Deploy**:
  - `daemon_process_running`: Confirmação de processo ativo.
  - `local_port_http_200`: Resposta 200 na porta 8088.
  - `audio_neural_pipeline_ready`: Inicialização correta dos modelos locais de transcrição (Whisper) e síntese.
  - `budget_fail_closed_validation`: Validação de que os limites do `PortfolioBudgetManager` estão ativos e respeitados ($0 em mock/local).
* **Salvaguarda de Rollback**:
  Finalização do processo com falha e reinicialização com o binário estável anterior.

---

## 4. Matriz Comparativa de Rollback

| Projeto | Gatilho de Falha | Ação de Rollback | RTO Máximo | RPO de Dados |
| :--- | :--- | :--- | :--- | :--- |
| **`darkfac`** | Falha de container, erro no DBOS PostgreSQL, falha no smoke pós-deploy | `docker compose -f deploy/dokploy/docker-compose.cloud.yml up -d --no-build` com o digest OCI anterior | 120s | RPO = 0 (volumes montados persistem estado do ControlStore) |
| **`site-ggcampos`** | Erro 404 em assets, quebra de layout, falha na auditoria de SEO | Reversão atômica de symlink ou redeploy FTP da pasta `dist/` anterior | 60s | RPO = 0 (aplicação estática pura) |
| **`segundo-cerebro`** | Falha no `PRAGMA integrity_check`, erro de dimensionalidade Chroma, queda do MCP | Restauração de snapshot SQLite em `.factory/backups/` e checkout do Git SHA anterior | 60s | RPO = 0 com replay de WAL |
| **`jarvis`** | Falha de resposta na porta 8088, travamento do pipeline de voz | Finalização do processo órfão e reinício do executável do SHA estável | 30s | RPO = 0 (serviço stateless) |

---

## 5. Handoff para Unidades Sucessoras

A entrega deste documento normativo e do artefato vinculado `.factory/planning/continuous-autonomy/bindings/release.json` cumpre os requisitos de arquitetura superior estabelecidos para o marco `HF-12-01` e desbloqueia os seguintes tickets no DAG de execução autônoma:

* **`HF-12-02`** (*Build real e adapter de deploy*):
  Implementação das classes `BuildService` e `DeploymentAdapter`, incorporando as interfaces de extração de OCI digest, despacho tRPC Dokploy e monitoramento de `external_operation_id` definidas nesta especificação.
* **`HF-15-01`** (*Observador base de telemetria e integridade*):
  Implementação dos observadores contínuos que coletam evidências operacionais e checam o estado dos containers e serviços locais.
* **`HF-03-07`** (*Preflight por host e projeto*):
  Validação das portas, permissões e endpoints fixados neste documento antes da ativação completa da suíte multi-projeto.
