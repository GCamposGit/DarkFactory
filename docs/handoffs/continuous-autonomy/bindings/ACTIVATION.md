# Guia de Ativação Operacional e Pré-Voo por Host e Projeto (HF-03-07)

Versão 1.0 · 19/09/2026 · Ticket de Alta Arquitetura `HF-03-07` · Parent `HF-03` · Prioridade P0.  
Handoff Normativo de Infraestrutura e Ativação Operacional da Dark Factory.  
Contratos canônicos associados: [CONTRACTS.md](../CONTRACTS.md) | [HF-03-07.md](../HF-03-07.md) | [RELEASE.md](RELEASE.md) | [CONTROL.md](CONTROL.md) | [EXECUTORS.md](EXECUTORS.md) | [BASELINE.md](BASELINE.md).

---

## 1. Visão Geral Operacional e Invariantes Inegociáveis

O ticket `HF-03-07` estabelece o binding de infraestrutura que governa a ativação operacional em nuvem e a homologação determinística de ambientes para a Dark Factory. Em alinhamento estrito com `ADR-HF-001`, `CONTRACTS.md` e as políticas `AGENTS.md` e `GEMINI.md`, a operação em produção sobre o host Hetzner CX23 e a orquestração via Dokploy PaaS obedecem aos seguintes princípios inegociáveis:

1. **"Notebook probe não certifica cloud"**:
   A execução bem-sucedida de testes unitários ou scripts em ambiente de desenvolvimento local (Windows) NÃO certifica o ambiente de nuvem. Toda alegação de prontidão operacional (`READINESS`) requer a execução de sondas reais contra os endpoints da VPS Hetzner.
2. **Manifesto Incompleto Bloqueia Deploy (Fail-Closed)**:
   Se qualquer variável obrigatória, referência de segredo, volume ou porta declarados no manifesto de infraestrutura (`deploy/dokploy/docker-compose.cloud.yml` e `environments.json`) estiver ausente ou incorretamente mapeada, o despacho de deploy deve ser abortado imediatamente sem efeitos colaterais.
3. **Zero Credenciais em Texto Claro**:
   Nenhuma credencial, token de API, senha de banco de dados ou chave privada pode ser persistida em texto plano no repositório, em commits do Git, em logs de build ou em variáveis de ambiente públicas. Todas as credenciais utilizam o contrato canônico `SecretReference(provider, locator, variable_name)`.
4. **Usuário Desprivilegiado Mandatório no Banco de Dados**:
   O banco de dados relacional PostgreSQL 16 interno deve operar com usuário aplicacional sem privilégios de superusuário (`darkfac_worker`). Conforme a política expressa do módulo `core.orchestrator.cloud_db`, a conexão como `postgres`, `root` ou `admin` é estritamente rejeitada pelo orquestrador.
5. **Limites de Recursos Conforme Capacidade do Host**:
   O host alvo (Hetzner CX23) dispõe de 4 GB de memória RAM e 2 vCPUs. Os limites impostos por serviço (`darkfac-coordinator` e `darkfac-worker`) limitam o consumo a 1.5 CPUs e 1536 MB de memória RAM cada, garantindo headroom operacional de ao menos 1 GB para o sistema operacional, Traefik e Dokploy PaaS.
6. **Alinhamento Estrito de Git SHA**:
   O código implantado no host deve ter como origem um SHA idêntico ou descendente válido do repositório remoto auditado (`GCamposGit/DarkFactory#main`). Deploys a partir de worktrees sujas ou commits locais não sincronizados configuram dependência técnica impeditiva.

---

## 2. Inventário Canônico dos Ambientes

### 2.1. Ambiente Local (`local_dev`)
* **Finalidade**: Desenvolvimento interativo, execução de testes unitários a custo $0 e operação offline.
* **Plataforma**: Windows 11 / Windows Server (`C:\dev\DarkFac`).
* **Runtime**: Python 3.12+ (ambiente virtual `C:\dev\DarkFac\.venv`).
* **Portas Locais**:
  - `8000`: DarkHub FastAPI & Webhook Gateway (`http://127.0.0.1:8000`)
  - `8001`: Supervisor Cloud Coordinator local (`http://127.0.0.1:8001`)
  - `8088`: Daemon do assistente local Jarvis (`http://127.0.0.1:8088`)
  - `11434`: Daemon de inferência local Ollama (`http://127.0.0.1:11434`)
* **Banco de Dados**: SQLite em modo WAL (`C:\dev\DarkFac\.factory\orchestrator_hf05.sqlite3` ou `:memory:`).
* **Provedor de Segredos**: Arquivo local `C:\dev\DarkFac\.env` ou variáveis de processo no Windows.

### 2.2. Ambiente de Produção Cloud (`hetzner_dokploy_prod`)
* **Finalidade**: Execução contínua 24/7, filas distribuídas, orquestração durável e gateway de webhooks.
* **Provedor e Host**: Hetzner Cloud VPS `darkfac-vps-primary` (Tipo CX23, Datacenter Falkenstein `fsn1-dc14`).
* **Hardware**: 2 vCPUs x86_64, 4096 MB RAM, 40 GB NVMe SSD.
* **Endereçamento de Rede**:
  - IPv4 Público: `178.105.73.168`
  - IPv4 Malha Segura Tailscale: `100.83.176.60`
  - Rede Interna Docker: `dokploy-network` (bridge externa gerenciada pelo Dokploy)
* **Domínios e Ingress TLS**:
  - Painel PaaS: `https://dokploy.ggcampos.com` (porta 3000 interna roteada via Traefik)
  - Serviço DarkHub: `https://darkhub.ggcampos.com` (porta 8000 interna com terminação TLS Traefik)
  - Porta Interna Coordinator: `8001` (comunicação restrita via `dokploy-network` ou localhost)
* **Banco de Dados**: PostgreSQL 16 interno na porta 5432 do container `dokploy-postgres`, banco `darkfac_db`, usuário `darkfac_worker`.
* **Volumes Nomeados e Persistência**:
  - `darkfac-artifacts`: montado em `/app/.factory/artifacts`
  - `darkfac-test-logs`: montado em `/app/.factory/test_logs`
  - Bind mount do host: `/var/lib/dokploy/data/darkfac` montado em `/app/data`

---

## 3. Pré-Requisitos de Acesso e Credenciais

Antes de iniciar a configuração na interface web do Dokploy ou executar comandos no terminal, certifique-se de reunir os seguintes acessos:

1. **Acesso SSH ao Servidor Hetzner**:
   - Chave privada Ed25519 instalada na estação de trabalho (`~/.ssh/id_ed25519`).
   - Acesso direto via IP público: `ssh root@178.105.73.168` ou via Tailscale: `ssh root@100.83.176.60`.
2. **Acesso Web ao Dokploy**:
   - URL: `https://dokploy.ggcampos.com`
   - Credenciais de administrador do Dokploy (configuradas no setup inicial do PaaS).
3. **Personal Access Token do GitHub (`GITHUB_PAT`)**:
   - Escopos mínimos requeridos: `repo` (leitura de repositórios privados e pacotes).
4. **Chaves de Provedores de IA**:
   - `OPENROUTER_API_KEY`: Chave da API OpenRouter para despacho de modelos de fronteira.
   - `OPENAI_API_KEY`: Chave da OpenAI para embeddings ou fallbacks opcionais.

---

## 4. Guia Passo a Passo Tela a Tela de Ativação no Dokploy

Siga rigorosamente as instruções abaixo. Não assuma valores padrão divergentes dos especificados.

```
+-----------------------------------------------------------------------------------+
|                        FLUXO DE ATIVAÇÃO TELA A TELA DOKPLOY                     |
+-----------------------------------------------------------------------------------+
| Tela 1: Login Dokploy  --> Autenticação em https://dokploy.ggcampos.com          |
| Tela 2: Projeto        --> Criação/Seleção do Projeto "Dark Factory"             |
| Tela 3: Banco Postgres --> Provisionamento do PostgreSQL interno (dokploy-postgres)|
| Tela 4: Usuário App    --> Criação de 'darkfac_worker' (não-root) e 'darkfac_db'  |
| Tela 5: Serviço Compose--> Criação do Serviço tipo 'Compose' (darkfac-cloud)      |
| Tela 6: Repositório Git--> Apontamento para GitHub e Docker Compose Cloud Path    |
| Tela 7: Variáveis .env --> Configuração exata das variáveis de ambiente e segredos|
| Tela 8: Domínio Traefik--> Configuração de darkhub.ggcampos.com com Let's Encrypt |
| Tela 9: Deploy e Logs  --> Acionamento de Build e visualização dos logs de runtime|
| Tela 10: Validação     --> Execução do Preflight determinístico e healthcheck     |
+-----------------------------------------------------------------------------------+
```

### Tela 1: Autenticação no Dokploy Dashboard
1. Abra o navegador web e acerte o endereço: `https://dokploy.ggcampos.com`.
2. No formulário de login:
   - Campo **Email**: digite o email de administração registrado do operador (ex: `gui.gcampos@gmail.com`).
   - Campo **Password**: digite a senha mestra do Dokploy.
3. Clique no botão **"Sign In"**.
4. Confirme que você está no painel principal (**Dashboard**) com os gráficos de CPU, Memória e Disco do servidor `darkfac-vps-primary`.

---

### Tela 2: Seleção ou Criação do Projeto
1. No menu lateral esquerdo, clique na opção **"Projects"**.
2. Verifique se o projeto **"Dark Factory"** já existe na lista.
3. Caso não exista:
   - Clique no botão superior direito **"+ Create Project"**.
   - No modal aberto:
     - Campo **Name**: digite exatamente `Dark Factory`.
     - Campo **Description**: digite `Infraestrutura autônoma de desenvolvimento e orquestração da Dark Factory`.
   - Clique em **"Create"**.
4. Clique no card do projeto **"Dark Factory"** para ingressar em sua visão de serviços.

---

### Tela 3: Provisionamento do Banco de Dados PostgreSQL Interno
1. Na aba de serviços do projeto, clique no botão **"+ Create Service"** ou selecione a categoria **"Databases"**.
2. Selecione a opção **"PostgreSQL"**.
3. Preencha os campos básicos:
   - Campo **Name**: digite `dokploy-postgres`.
   - Campo **Database Version**: selecione no dropdown a versão `16` (ou `16-alpine`).
   - Campo **Database Name**: digite temporariamente `postgres` (o banco da aplicação será criado no passo seguinte).
   - Campo **User**: `postgres` (usuário administrativo de provisionamento inicial).
   - Campo **Password**: gere uma senha forte e anote com segurança no gerenciador de senhas.
4. Na seção **Network**:
   - Confirme que a rede selecionada é `dokploy-network`.
5. Clique em **"Deploy Database"**.
6. Aguarde até que o status do container transicione para **"Running"** (verde).

---

### Tela 4: Criação do Banco `darkfac_db` e Usuário Não-Root `darkfac_worker`
> [!IMPORTANT]
> A política HF-03 veda estritamente o uso do superusuário `postgres` na aplicação. O procedimento a seguir garante o princípio do menor privilégio.

1. No menu lateral, acesse o terminal do container do PostgreSQL clicando no botão **"Terminal"** do serviço `dokploy-postgres` (ou abra um terminal SSH no host).
2. Execute o comando `psql` para abrir o console SQL administrativo:
   ```bash
   psql -U postgres
   ```
3. Digite e execute os comandos SQL abaixo, substituindo `<SENHA_FORTE_WORKER>` por uma senha aleatória segura (mínimo 24 caracteres):
   ```sql
   -- 1. Cria o banco de dados da aplicação
   CREATE DATABASE darkfac_db;

   -- 2. Cria o usuário aplicacional desprivilegiado
   CREATE USER darkfac_worker WITH PASSWORD '<SENHA_FORTE_WORKER>';

   -- 3. Concede privilégios restritos ao banco darkfac_db
   GRANT ALL PRIVILEGES ON DATABASE darkfac_db TO darkfac_worker;

   -- 4. Conecta ao banco recém-criado e ajusta os privilégios de schema
   \c darkfac_db
   GRANT ALL ON SCHEMA public TO darkfac_worker;
   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO darkfac_worker;
   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO darkfac_worker;

   -- 5. Sai do psql
   \q
   ```
4. A connection string estruturada canônica resultante será:
   ```
   postgresql://darkfac_worker:<SENHA_FORTE_WORKER>@dokploy-postgres:5432/darkfac_db
   ```
   *(Esta string será inserida como SecretReference na Tela 7)*.

---

### Tela 5: Criação do Serviço Docker Compose no Dokploy
1. Retorne à página principal do projeto **"Dark Factory"**.
2. Clique no botão **"+ Create Service"**.
3. Na lista de tipos de serviço, selecione **"Compose"**.
4. No formulário:
   - Campo **Name**: digite `darkfac-cloud`.
   - Campo **Description**: digite `Dark Factory Cloud Coordinator & Isolated Worker Stack`.
5. Clique em **"Create"**.
6. O Dokploy redirecionará para a página de configuração do serviço `darkfac-cloud`.

---

### Tela 6: Configuração da Fonte (Git Repository)
1. Na aba superior do serviço `darkfac-cloud`, clique na aba **"General"** ou **"Source"**.
2. Configure os seletores:
   - Seletor **Source Type**: selecione `Git`.
   - Campo **Repository URL**: digite `https://github.com/GCamposGit/DarkFactory.git`.
   - Campo **Branch**: digite `main`.
   - Seletor **Build Type**: selecione `Docker Compose`.
   - Campo **Compose Path**: digite exatamente `deploy/dokploy/docker-compose.cloud.yml`.
   - Campo **Auto Deploy (Webhook)**: marque o toggle como **ON** (ativo) para permitir atualizações automáticas após aprovação em pipeline.
3. Clique em **"Save"**.

---

### Tela 7: Configuração de Variáveis de Ambiente e Segredos
1. Na barra de navegação do serviço `darkfac-cloud`, clique na aba **"Environment"**.
2. Selecione o modo **"Raw Editor"** ou preencha linha a linha na tabela.
3. Cole as seguintes variáveis com seus respectivos valores (substituindo os placeholders de segredo):

| Nome da Variável | Tipo / Seletor | Valor Recomendado / Conteúdo | Descrição e Justificativa |
| :--- | :--- | :--- | :--- |
| `DARKFAC_HF02_DATABASE_URL` | **Secret (Encrypt)** | `postgresql://darkfac_worker:<SENHA_FORTE_WORKER>@dokploy-postgres:5432/darkfac_db` | DSN de conexão ao PostgreSQL interno pelo usuário não-root. |
| `DARKFAC_MAX_CONCURRENT_SLOTS` | Plain Text | `2` | Limite de slots de execução simultânea para proteger a memória do VPS. |
| `DARKFAC_COORDINATOR_PORT` | Plain Text | `8001` | Porta TCP interna em que o Cloud Coordinator atende requisições. |
| `DARKFAC_WORKER_ID` | Plain Text | `cloud-worker-1` | Identificador estático do worker em nuvem. |
| `GITHUB_PAT` | **Secret (Encrypt)** | `ghp_ExemploTokenDeAcessoAoRepositorioPrivado` | Token pessoal do GitHub para clone autenticado no Dockerfile. |
| `OPENROUTER_API_KEY` | **Secret (Encrypt)** | `sk-or-v1-ExemploChaveOpenRouter` | Chave de inferência dos modelos de nuvem. |
| `OPENAI_API_KEY` | **Secret (Encrypt)** | `sk-proj-ExemploChaveOpenAI` | Chave opcional de fallback OpenAI. |
| `OLLAMA_BASE_URL` | Plain Text | `http://100.81.84.124:11434` | Endpoint Ollama da estação de trabalho na malha Tailscale. |
| `DARKHUB_ALLOWED_HOSTS` | Plain Text | `darkhub.ggcampos.com,127.0.0.1,localhost,100.83.176.60` | Lista de hosts autorizados no cabeçalho HTTP Host. |
| `CLOUDFLARE_ZERO_TRUST_REQUIRED`| Toggle | `false` | Defina como `false` inicialmente até validação de DNS/TLS. |
| `DATA_DIR` | Plain Text | `/app/data` | Diretório interno de dados persistentes montados. |
| `FACTORY_DIR` | Plain Text | `/app/.factory` | Diretório interno de artefatos e relatórios. |

4. Certifique-se de marcar a caixa de seleção **"Encrypt / Secret"** para as variáveis sensíveis (`DARKFAC_HF02_DATABASE_URL`, `GITHUB_PAT`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`).
5. Clique no botão **"Save Variables"**.

---

### Tela 8: Configuração de Domínio e Ingress no Traefik
1. Na barra superior do serviço `darkfac-cloud`, clique na aba **"Domains"**.
2. Clique em **"+ Add Domain"**.
3. Preencha os campos da rota:
   - Campo **Host**: digite exatamente `darkhub.ggcampos.com`.
   - Campo **Path**: digite `/`.
   - Campo **Container**: selecione `darkfac-coordinator` (ou o serviço DarkHub quando mapeado).
   - Campo **Container Port**: selecione `8000`.
   - Toggle **HTTPS (Let's Encrypt)**: marque como **ON** (Ativado).
   - Campo **Certificate Resolver**: selecione `letsencrypt`.
   - Toggle **Redirect HTTP to HTTPS**: marque como **ON** (Ativado).
4. Clique em **"Save Domain"**.
5. O Traefik gerará automaticamente o desafio HTTP-01 com o Let's Encrypt para emitir o certificado TLS.

---

### Tela 9: Deploy Inicial e Acompanhamento de Logs
1. Na parte superior direita da tela do serviço `darkfac-cloud`, clique no botão **"Deploy"**.
2. Uma gaveta lateral ou modal de logs será aberta exibindo o progresso do build:
   - Execução do clone via contexto Git com o `GITHUB_PAT`.
   - Compilação dos layers baseados em Python 3.12-slim no Dockerfile.
   - Instalação dos requisitos e da biblioteca DarkFac.
   - Criação dos containers `darkfac-coordinator` e `darkfac-worker-1`.
3. Verifique as últimas linhas de log. As seguintes mensagens indicam sucesso operacional:
   ```text
   [INFO] darkfac.cloud_coordinator: Cloud coordinator started supervision loop (poll_interval=5.0s)
   [INFO] darkfac.cloud_worker: Cloud worker cloud-worker-1 started (slots=2)
   ```
4. Se o container apresentar `Exited (1)`, verifique a aba de variáveis de ambiente e a conectividade com o banco de dados.

---

### Tela 10: Verificação de Healthcheck e Telemetria
1. Abra uma nova aba no navegador e acesse: `https://darkhub.ggcampos.com/health`.
2. O retorno esperado deve ser um JSON com HTTP status 200:
   ```json
   {
     "status": "healthy",
     "coordinator_alive": true,
     "database_status": "ready",
     "version": "v1"
   }
   ```
3. Retorne à interface do Dokploy e confirme que o badge de status do serviço `darkfac-cloud` exibe **"Running"** em cor verde.

---

## 5. Comandos Canônicos Operacionais

> [!NOTE]
> Em conformidade estrita com a regra `AGENTS.md` e `GEMINI.md`, todos os comandos indicados para execução no terminal Windows utilizam endereços absolutos completos para evitar qualquer erro de diretório relativo (`FileNotFoundError`).

### 5.1. Comandos de Operação na Nuvem (Host Hetzner CX23 / Dokploy)

#### 1. Instalação e Preparação da Infraestrutura (`install`):
Execute a partir do terminal da sua estação conectando via SSH:
```bash
ssh -i ~/.ssh/id_ed25519 root@178.105.73.168 'docker network create dokploy-network || true && docker volume create darkfac-artifacts && docker volume create darkfac-test-logs && mkdir -p /var/lib/dokploy/data/darkfac'
```

#### 2. Inicialização / Deploy da Stack (`start`):
```bash
ssh -i ~/.ssh/id_ed25519 root@178.105.73.168 'cd /etc/dokploy/compose/darkfac-cloud && docker compose -f docker-compose.cloud.yml up -d --build'
```

#### 3. Inspeção de Status Operacional (`status`):
```bash
ssh -i ~/.ssh/id_ed25519 root@178.105.73.168 'docker ps --filter "name=darkfac" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" && docker exec darkfac-coordinator python -m core.orchestrator.cloud_coordinator --status'
```

#### 4. Reinicialização de Serviços (`restart`):
```bash
ssh -i ~/.ssh/id_ed25519 root@178.105.73.168 'docker compose -f /etc/dokploy/compose/darkfac-cloud/docker-compose.cloud.yml restart darkfac-coordinator darkfac-worker'
```

#### 5. Rollback Imediato para Imagem Anterior (`rollback`):
Em caso de falha de jornada sintética ou regressão em produção, restaura o container sem rebuild:
```bash
ssh -i ~/.ssh/id_ed25519 root@178.105.73.168 'cd /etc/dokploy/compose/darkfac-cloud && docker compose -f docker-compose.cloud.yml down && docker compose -f docker-compose.cloud.yml up -d --no-build'
```

---

### 5.2. Comandos de Operação no Ambiente de Desenvolvimento Local (Windows)

#### 1. Instalação de Dependências e Ambiente Virtual (`install`):
```powershell
python -m venv C:\dev\DarkFac\.venv
C:\dev\DarkFac\.venv\Scripts\python.exe -m pip install --upgrade pip
C:\dev\DarkFac\.venv\Scripts\python.exe -m pip install -e C:\dev\DarkFac
```

#### 2. Inicialização do DarkHub Local (`start`):
```powershell
C:\dev\DarkFac\.venv\Scripts\python.exe -m uvicorn hub.backend.main:app --host 127.0.0.1 --port 8000 --reload
```

#### 3. Verificação de Status do Coordinator Local (`status`):
```powershell
C:\dev\DarkFac\.venv\Scripts\python.exe C:\dev\DarkFac\core\orchestrator\cloud_coordinator.py --status
```

#### 4. Reinicialização dos Processos Locais (`restart`):
```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object {$_.Path -like '*DarkFac*'} | Stop-Process -Force; Start-Process C:\dev\DarkFac\.venv\Scripts\python.exe -ArgumentList '-m uvicorn hub.backend.main:app --host 127.0.0.1 --port 8000' -WindowStyle Minimized"
```

#### 5. Rollback Local para Commit Estável (`rollback`):
```powershell
git -C C:\dev\DarkFac checkout 83e5298eb231599076811802dceac8575c7f6feb
```

---

## 6. Procedimentos de Teste e Preflight de Rede, Portas e TLS

Para garantir a ausência de regressões antes do tráfego de produção, execute os seguintes testes determinísticos:

### 6.1. Pré-Voo de Conectividade de Rede e SSH
Execute no PowerShell local:
```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Test-NetConnection -ComputerName 178.105.73.168 -Port 22; Test-NetConnection -ComputerName 100.83.176.60 -Port 22"
```
* **Oráculo de Aceite**: Ambos devem retornar `TcpTestSucceeded : True`.

### 6.2. Pré-Voo do Banco de Dados PostgreSQL (Boundary de Superusuário)
Execute a partir do ambiente Python do projeto:
```powershell
C:\dev\DarkFac\.venv\Scripts\python.exe -m pytest C:\dev\DarkFac\tests\test_cloud_db.py -v
```
* **Oráculo de Aceite**: 100% dos testes devem passar. O teste assegura que usuários como `postgres` disparam erro estruturado e senhas são estritamente mascaradas (`***`).

### 6.3. Pré-Voo de Certificados TLS e Ingress Traefik
Execute no terminal da estação de trabalho:
```bash
curl -Iv --resolve darkhub.ggcampos.com:443:178.105.73.168 https://darkhub.ggcampos.com/health
```
* **Oráculo de Aceite**:
  - `SSL certificate verify ok`.
  - Certificado emitido por `Let's Encrypt Authority`.
  - Resposta HTTP com código de status `200 OK`.

### 6.4. Pré-Voo de Limites de Memória e Docker Compose
Execute a suíte focal de validação do manifesto:
```powershell
C:\dev\DarkFac\.venv\Scripts\python.exe -m pytest C:\dev\DarkFac\tests\test_cloud_deployment_manifest.py -v
```
* **Oráculo de Aceite**: Validação de que limites de 1536M por container e rede externa `dokploy-network` estão estritamente definidos.

---

## 7. Matriz de Troubleshooting e Recuperação de Desastres

| Sintoma Observado | Causa Mais Provável | Ação Determinística de Correção |
| :--- | :--- | :--- |
| Erro `Superuser 'postgres' is forbidden by HF-03 policy` | `DARKFAC_HF02_DATABASE_URL` configurada com o usuário administrativo `postgres`. | Acesse a Tela 4, crie o usuário `darkfac_worker`, conceda privilégios no `darkfac_db` e atualize a variável no Dokploy (Tela 7). |
| Dokploy Deploy falha no passo de clone do Git | Token `GITHUB_PAT` ausente, inválido ou expirado. | Gere um novo Personal Access Token no GitHub com escopo `repo` e atualize a variável `GITHUB_PAT` nas variáveis do serviço no Dokploy. |
| Container reinicia em loop (`CrashLoopBackOff`) | PostgreSQL inacessível ou falha de resolução de nome de host `dokploy-postgres`. | Verifique se o container do banco está na rede `dokploy-network` executando: `docker network connect dokploy-network dokploy-postgres`. |
| Erro de TLS no navegador (`SSL_ERROR_BAD_CERT_DOMAIN`) | Desafio ACME do Let's Encrypt ainda não concluiu ou DNS não propagou. | Verifique se as entradas DNS Tipo A de `darkhub.ggcampos.com` apontam para `178.105.73.168`. Consulte os logs do Traefik no Dokploy. |
| Falha de memória / Kernel OOM Killer no VPS | Consumo combinado de serviços ultrapassou 3.5 GB na VPS CX23. | Verifique containers em execução via `docker stats`. Os limites de 1536 MB previnem OOM se respeitados estritamente. |

---

## 8. Conclusão e Próximos Passos do Roadmap

A homologação e ativação dos contratos deste documento cumprem integralmente as exigências do ticket **HF-03-07**.  
O ticket sucessor imediato é:
- **`HF-03-08`** — Ativação isolada e fatia vertical em ambiente de teste real.
