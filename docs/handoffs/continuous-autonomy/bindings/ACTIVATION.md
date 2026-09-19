# Guia de Ativação Operacional e Pré-Voo por Host e Projeto (HF-03-07)

Versão 1.1 · 19/09/2026 · Ticket de Alta Arquitetura `HF-03-07` · Parent `HF-03` · Prioridade P0.  
Handoff Normativo de Infraestrutura e Ativação Operacional da Dark Factory.  
Contratos canônicos associados: [CONTRACTS.md](../CONTRACTS.md) | [HF-03-07.md](../HF-03-07.md) | [RELEASE.md](RELEASE.md) | [CONTROL.md](CONTROL.md) | [EXECUTORS.md](EXECUTORS.md) | [BASELINE.md](BASELINE.md).

---

## 1. Mapeamento da Infraestrutura Real Existente

Para eliminar qualquer risco de retrabalho ou duplicidade de serviços, este guia está rigorosamente ancorado nos recursos que **já estão provisionados** no seu Dokploy:

| Componente | Status Atual | Nome / Identificador Real | Observação Operacional |
| :--- | :--- | :--- | :--- |
| **PaaS Dokploy** | Ativo | `https://dokploy.ggcampos.com` | Hetzner CX23 (`darkfac-vps-primary`) |
| **Projeto Dokploy** | Já Criado | `darkfac-core` | Projeto raiz que agrupa os serviços da fábrica |
| **Serviço PostgreSQL**| Já Provisionado | `postgres-primary` | Container PostgreSQL 16 na rede `dokploy-network` |
| **Banco de Dados** | Já Criado | `darkfac_hf02_prod` | Banco relacional para DBOS e ControlStore |
| **Usuário do Banco** | Já Criado | `darkfac_worker` | Usuário desprivilegiado aplicacional (não-root) |
| **Serviço Compose** | **A Criar / Deploy** | `darkfac-cloud` | Stack Coordinator + Worker permanente da fábrica |

---

## 2. Invariantes Inegociáveis

1. **"Notebook probe não certifica cloud"**:
   A execução bem-sucedida de testes unitários ou scripts em ambiente de desenvolvimento local (Windows) NÃO certifica o ambiente de nuvem. Toda alegação de prontidão operacional (`READINESS`) requer a execução de sondas reais contra os endpoints da VPS Hetzner.
2. **Reuso Estrito da Infraestrutura**:
   NENHUM novo serviço de banco de dados ou novo projeto deve ser criado. Toda a orquestração opera sobre `darkfac-core`, `postgres-primary` e `darkfac_hf02_prod`.
3. **Zero Credenciais em Texto Claro**:
   Nenhuma credencial, token de API, senha de banco de dados ou chave privada pode ser persistida em texto plano no repositório. As variáveis no Dokploy devem usar a proteção de segredo (*Encrypt*).
4. **Usuário Desprivilegiado Mandatório no Banco**:
   O banco de dados relacional opera estritamente com `darkfac_worker`. Conforme a política do módulo `core.orchestrator.cloud_db`, a conexão direta como `postgres`, `root` ou `admin` é terminantemente rejeitada pela fábrica.
5. **Limites de Recursos Conforme Capacidade do Host**:
   O host Hetzner CX23 dispõe de 4 GB de memória RAM e 2 vCPUs. Os limites impostos por serviço (`darkfac-coordinator` e `darkfac-worker`) restringem o consumo a 1.5 CPUs e 1536 MB de RAM cada, garantindo headroom operacional de ao menos 1 GB para o sistema operacional, Traefik e Dokploy.

---

## 3. Verificação de Privilégios do `darkfac_worker` no PostgreSQL

Como o usuário `darkfac_worker` e o banco `darkfac_hf02_prod` já existem no `postgres-primary`, execute a rápida conferência abaixo para garantir que o usuário possui as permissões necessárias para criar tabelas de controle e workflows:

1. No Dokploy, acerte o projeto **`darkfac-core`** e clique no serviço de banco de dados **`postgres-primary`**.
2. Clique na aba ou botão **"Terminal"** (ou conecte via SSH no host Hetzner).
3. Conecte ao console administrativo do Postgres:
   ```bash
   psql -U postgres
   ```
4. Execute as 4 linhas abaixo para garantir os privilégios no banco `darkfac_hf02_prod`:
   ```sql
   \c darkfac_hf02_prod
   GRANT ALL PRIVILEGES ON DATABASE darkfac_hf02_prod TO darkfac_worker;
   GRANT ALL ON SCHEMA public TO darkfac_worker;
   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO darkfac_worker;
   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO darkfac_worker;
   ```
5. Saia do console:
   ```sql
   \q
   ```

A connection string estruturada canônica é:
```
postgresql://darkfac_worker:<SUA_SENHA_WORKER>@postgres-primary:5432/darkfac_hf02_prod
```

---

## 4. Guia Passo a Passo no Dokploy: Deploy do Serviço Compose

Agora que o banco já está pronto, basta criar o serviço Docker Compose no projeto `darkfac-core`:

```
+-----------------------------------------------------------------------------------+
|                        FLUXO DE CONFIGURAÇÃO NO DOKPLOY                           |
+-----------------------------------------------------------------------------------+
| Passo 1: Abrir Projeto      --> Acessar projeto existente 'darkfac-core'          |
| Passo 2: Criar Serviço      --> Adicionar novo serviço tipo 'Compose'             |
| Passo 3: Configurar Git     --> Apontar repositório e caminho do compose cloud    |
| Passo 4: Variáveis (.env)   --> Configurar variáveis com a connection string real |
| Passo 5: Deploy e Logs      --> Acionar o deploy e verificar logs dos daemons     |
+-----------------------------------------------------------------------------------+
```

### Passo 1: Acessar o Projeto Existente
1. Acesse `https://dokploy.ggcampos.com` e efetue login.
2. No menu lateral, clique em **"Projects"**.
3. Clique no projeto existente: **`darkfac-core`**.

### Passo 2: Criar o Serviço Docker Compose
1. Dentro do projeto `darkfac-core`, clique em **"+ Create Service"**.
2. Selecione a opção **"Compose"**.
3. No formulário:
   - Campo **Name**: digite `darkfac-cloud`
   - Campo **Description**: `Dark Factory Cloud Coordinator & Isolated Worker Stack`
4. Clique em **"Create"**.

### Passo 3: Configurar a Fonte do Repositório (Git)
1. Na página do serviço `darkfac-cloud`, clique na aba **"General"** (ou **"Source"**).
2. Preencha os campos exatamente assim:
   - **Source Type**: `Git`
   - **Repository URL**: `https://github.com/GCamposGit/DarkFactory.git`
   - **Branch**: `main`
   - **Build Type**: `Docker Compose`
   - **Compose Path**: `deploy/dokploy/docker-compose.cloud.yml`
   - **Auto Deploy**: Ative o toggle (**ON**) se desejar deploys automáticos via webhook
3. Clique em **"Save"**.

### Passo 4: Configurar Variáveis de Ambiente e Segredos
1. Clique na aba **"Environment"** do serviço `darkfac-cloud`.
2. Insira as variáveis abaixo (utilizando os valores reais da sua infraestrutura):

| Nome da Variável | Tipo / Seletor | Valor Recomendado / Conteúdo | Descrição |
| :--- | :--- | :--- | :--- |
| `DARKFAC_HF02_DATABASE_URL` | **Secret (Encrypt)** | `postgresql://darkfac_worker:<SUA_SENHA>@postgres-primary:5432/darkfac_hf02_prod` | DSN de conexão ao `postgres-primary` interno. |
| `DARKFAC_MAX_CONCURRENT_SLOTS` | Plain Text | `2` | Limite de slots de concorrência simultânea. |
| `DARKFAC_COORDINATOR_PORT` | Plain Text | `8001` | Porta interna do Cloud Coordinator. |
| `DARKFAC_WORKER_ID` | Plain Text | `cloud-worker-1` | Identificador do worker na nuvem. |
| `GITHUB_PAT` | **Secret (Encrypt)** | `<SEU_TOKEN_GITHUB>` | Token para clone autenticado no build do Dockerfile. |
| `OPENROUTER_API_KEY` | **Secret (Encrypt)** | `<SUA_CHAVE_OPENROUTER>` | Chave de inferência dos modelos de nuvem. |
| `OPENAI_API_KEY` | **Secret (Encrypt)** | `<SUA_CHAVE_OPENAI>` | Chave opcional OpenAI. |
| `OLLAMA_BASE_URL` | Plain Text | `http://100.81.84.124:11434` | Endpoint Ollama da máquina local na malha Tailscale. |
| `DARKHUB_ALLOWED_HOSTS` | Plain Text | `darkhub.ggcampos.com,127.0.0.1,localhost,100.83.176.60` | Hosts permitidos pelo middleware de segurança. |
| `DATA_DIR` | Plain Text | `/app/data` | Diretório interno de dados persistentes. |
| `FACTORY_DIR` | Plain Text | `/app/.factory` | Diretório interno de artefatos da fábrica. |

3. Marque o toggle **"Encrypt / Secret"** para as variáveis sensíveis (`DARKFAC_HF02_DATABASE_URL`, `GITHUB_PAT`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`).
4. Clique em **"Save Variables"**.

### Passo 5: Deploy e Acompanhamento (Pule a aba Domains)
> [!NOTE]
> O serviço `darkfac-cloud` é uma stack de backend puro (Coordinator e Worker). Ele roda em segundo plano, executando tarefas e se comunicando internamente com o banco `postgres-primary`. Portanto, **não adicione domínio na aba Domains** (deixe em branco). O domínio `darkhub.ggcampos.com` pertence exclusivamente ao serviço do dashboard web DarkHub (`docker-compose.hub.yml`).

1. No canto superior direito da tela do serviço `darkfac-cloud`, clique no botão **"Deploy"**.
2. Acompanhe os logs de build. Os containers `darkfac-coordinator` e `darkfac-worker-1` subirão conectados à rede `dokploy-network`.
3. Verifique as linhas finais de log com o início dos daemons:
   ```text
   [INFO] darkfac.cloud_coordinator: Cloud coordinator started supervision loop (poll_interval=5.0s)
   [INFO] darkfac.cloud_worker: Cloud worker cloud-worker-1 started (slots=2)
   ```

---

## 5. Comandos Canônicos Operacionais

> [!NOTE]
> Todos os comandos no Windows utilizam caminhos absolutos completos para eliminar qualquer erro de diretório relativo (`FileNotFoundError`).

### 5.1. Comandos no Host Hetzner CX23 (via SSH)

- **Verificar status dos containers**:
  ```bash
  ssh -i ~/.ssh/id_ed25519 root@178.105.73.168 'docker ps --filter "name=darkfac" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"'
  ```

- **Verificar diagnóstico estruturado do Coordinator**:
  ```bash
  ssh -i ~/.ssh/id_ed25519 root@178.105.73.168 'docker exec darkfac-coordinator python -m core.orchestrator.cloud_coordinator --status'
  ```

- **Reiniciar os daemons cloud**:
  ```bash
  ssh -i ~/.ssh/id_ed25519 root@178.105.73.168 'docker restart darkfac-coordinator darkfac-worker-1'
  ```

---

## 6. Procedimentos de Teste e Preflight

### 6.1. Pré-Voo do Banco de Dados PostgreSQL (Boundary de Superusuário)
Execute no terminal local no Windows:
```powershell
python -m pytest C:\dev\DarkFac\tests\test_cloud_db.py -v
```
* **Oráculo de Aceite**: 100% dos testes devem passar. Garante que `darkfac_worker` é aceito e superusuários são rejeitados de forma fail-closed.

### 6.2. Pré-Voo de Entrada no Dokploy e Healthcheck
Execute no terminal local:
```powershell
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "curl.exe -Iv https://darkhub.ggcampos.com/health"
```
* **Oráculo de Aceite**: Código de status HTTP `200 OK` e certificado SSL emitido por Let's Encrypt.
