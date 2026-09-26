# Runbook — Pipelines de Deploy Contínuo (Git Push Webhooks & Dokploy)

Este runbook instrui o Owner passo a passo, tela por tela e à prova de falhas, sobre como configurar o fluxo de **Deploy Contínuo sem Downtime** acionado por `git push` no repositório **GCamposGit/DarkFactory**, integrado à VPS Hetzner via **Dokploy PaaS** e **DarkHub Cloud Gateway**.

Não assumimos nenhuma experiência prévia com a interface do GitHub Webhooks ou Dokploy. Siga as instruções na ordem descrita.

---

## Índice

1. [Visão Geral da Arquitetura](#1-visão-geral-da-arquitetura)
2. [Passo 1: Obter a URL de Deploy no Dokploy](#passo-1-obter-a-url-de-deploy-no-dokploy)
3. [Passo 2: Configurar o Secret no GitHub Actions (Fluxo Seguro Pós-CI)](#passo-2-configurar-o-secret-no-github-actions-fluxo-seguro-pós-ci)
4. [Passo 3: Configurar o Webhook no GitHub (Fluxo Direto / DarkHub Gateway)](#passo-3-configurar-o-webhook-no-github-fluxo-direto--darkhub-gateway)
5. [Passo 4: Verificação Operacional e Teste Ponta a Ponta](#passo-4-verificação-operacional-e-teste-ponta-a-ponta)
6. [Solução de Problemas Comuns](#solução-de-problemas-comuns)

---

## 1. Visão Geral da Arquitetura

O deploy contínuo pode operar em dois modos complementares:

```mermaid
flowchart TD
    GitPush["Git Push na branch 'main'"] --> Choice{"Mecanismo de Disparo"}
    Choice -->|Opção A (Recomendada): Pós-CI| CI["GitHub Actions (ci.yml)<br/>Executa runner.py --quick"]
    CI -->|Testes Aprovados [HARNESS_PASS]| TriggerAction["Job: continuous-deployment<br/>Dispara Webhook Dokploy"]
    TriggerAction --> DokployPaaS["Dokploy PaaS (VPS Hetzner)<br/>Zero-Downtime Redeploy"]

    Choice -->|Opção B: Direto 24/7| DarkHubGateway["DarkHub Webhook Gateway<br/>(https://darkhub.ggcampos.com/api/webhooks/github)"]
    DarkHubGateway -->|Valida HMAC-SHA256 & Idempotência| DokployPaaS
```

- **Opção A (Pós-CI - Recomendada)**: O GitHub Actions executa toda a suíte de testes determinísticos. Somente se o commit for 100% aprovado, ele aciona o Dokploy. Evita publicar código quebrado.
- **Opção B (DarkHub Gateway 24/7)**: O GitHub envia o evento `push` diretamente para o DarkHub, que valida a assinatura criptográfica HMAC-SHA256, deduplica requisições contra ataques de replay e audita tudo em `.factory/webhooks/events.json`.

---

## Passo 1: Obter a URL de Deploy no Dokploy

1. Abra o navegador e acesse o painel do Dokploy: `https://dokploy.ggcampos.com`.
2. Faça login com suas credenciais de Owner.
3. No menu lateral esquerdo, clique em **Projects**.
4. Selecione o projeto **`darkfac-core`**.
5. Na lista de serviços do projeto, localize o serviço de compose ou container que deseja atualizar (por exemplo, `darkhub` ou `darkfac-compose`).
6. Clique no card do serviço para abrir suas configurações.
7. Localize a aba **Deployments** (ou **General** / **Webhook** dependendo da versão do Dokploy).
8. Procure pela seção rotulada como **Deploy Webhook** ou **Auto Deploy Webhook**.
9. Se a URL ainda não estiver gerada, clique no botão **Generate Webhook** / **Enable Webhook**.
10. A URL gerada terá o seguinte formato:
    ```text
    https://dokploy.ggcampos.com/api/deploy/compose/khkMuDfjN_0OtOGWTBet2
    ```
11. Clique no botão de copiar (ícone de prancheta) ao lado da URL e guarde-a temporariamente em seu bloco de notas.

---

## Passo 2: Configurar o Secret no GitHub Actions (Fluxo Seguro Pós-CI)

Este passo habilita o deploy automático após os testes passarem na branch `main`:

1. No navegador, acesse o repositório no GitHub: `https://github.com/GCamposGit/DarkFactory`.
2. Na barra de navegação superior do repositório, clique na aba **Settings** (ícone de engrenagem).
3. No menu lateral esquerdo, localize a seção **Security** e clique em **Secrets and variables** para expandir.
4. No submenu expandido, clique em **Actions**.
5. Na tela *Actions secrets and variables*, clique no botão verde **New repository secret** (canto superior direito).
6. Preencha os campos exatamente como indicado abaixo:
   - **Name**:
     ```text
     DOKPLOY_DEPLOY_URL
     ```
   - **Secret**:
     Cole a URL completa copiada no **Passo 1** (exemplo: `https://dokploy.ggcampos.com/api/deploy/compose/...`).
7. Clique no botão verde **Add secret**.
8. A partir deste momento, qualquer push para a branch `main` que passe com sucesso no job `main-validation` acionará automaticamente a compilação e deploy no Dokploy.

---

## Passo 3: Configurar o Webhook no GitHub (Fluxo Direto / DarkHub Gateway)

Para habilitar a recepção de eventos diretamente no gateway autônomo do DarkHub 24/7 com auditoria durável:

1. No repositório GitHub (`https://github.com/GCamposGit/DarkFactory`), ainda na aba **Settings**.
2. No menu lateral esquerdo, na seção **Code and automation**, clique em **Webhooks**.
3. No canto superior direito da página, clique no botão **Add webhook**.
4. Configure rigorosamente cada campo do formulário:
   - **Payload URL**:
     ```text
     https://darkhub.ggcampos.com/api/webhooks/github
     ```
     *(Se desejar acionar o Dokploy diretamente sem passar pelo DarkHub, cole aqui a URL do Passo 1)*.
   - **Content type**:
     Abra o seletor dropdown e selecione obrigatoriamente:
     ```text
     application/json
     ```
     *(ATENÇÃO: Não deixe em `application/x-www-form-urlencoded`, pois a verificação de assinatura HMAC exige o payload em JSON puro).*
   - **Secret**:
     Digite ou gere uma senha segura (chave secreta compartilhada).
     Sugestão de valor: utilize a mesma chave definida na variável `GITHUB_WEBHOOK_SECRET` do arquivo de ambiente do DarkHub (`deploy/dokploy/env.hub.example`).
   - **SSL verification**:
     Mantenha selecionada a opção recomendada:
     `Enable SSL verification` (o certificado Let's Encrypt do Dokploy/Cloudflare é válido e obrigatório).
   - **Which events would you like to trigger this webhook?**:
     Selecione a opção:
     - `Just the push event` (apenas pushes para deploy contínuo), OU
     - `Let me select individual events`: marque `Pushes` e `Pull requests` (caso deseje também auditoria de PRs sob a política DF-20).
   - **Active**:
     Certifique-se de que a caixa de seleção `Active` está marcada (com um tique verde).
5. Clique no botão verde **Add webhook** no rodapé da página.

---

## Passo 4: Verificação Operacional e Teste Ponta a Ponta

Para verificar que a integração está operando sem aguardar um commit em produção:

### 1. Teste de Handshake pelo GitHub
1. Na lista de webhooks em `Settings` -> `Webhooks`, clique no webhook recém-criado.
2. Role até a aba **Recent Deliveries**.
3. Você verá a entrega inicial do evento `ping`.
4. Um ícone de tique verde (`200 OK`) indica que o DarkHub ou Dokploy recebeu, autenticou a assinatura HMAC e respondeu com sucesso.

### 2. Simulação Local Headless via Terminal (Zero Interface Gráfica)
No PowerShell, execute o teste de validação do gateway utilizando o caminho absoluto:

```powershell
python -m pytest C:\dev\DarkFac\tests\test_darkhub_cloud_gateway.py -v
```

Deve retornar `17 passed` com 100% de cobertura das rotas de HMAC, idempotência e multi-serviços.

### 3. Consulta da Trilha de Auditoria via DarkHub API
Para consultar os eventos auditados em tempo real:

```powershell
Invoke-RestMethod -Uri "https://darkhub.ggcampos.com/api/webhooks/events?limit=5" -Method Get
```

---

## Solução de Problemas Comuns

| Sintoma | Causa Provável | Solução |
| :--- | :--- | :--- |
| **HTTP 401 Unauthorized** no GitHub Deliveries | Segredo divergente entre o GitHub e a VPS. | Verifique se a variável `GITHUB_WEBHOOK_SECRET` no container do DarkHub corresponde exatamente ao campo `Secret` preenchido no GitHub. |
| **Status `skipped`** no log do DarkHub | `DOKPLOY_DEPLOY_URL` não configurada. | Defina a variável de ambiente `DOKPLOY_DEPLOY_URL` (ou `DOKPLOY_DEPLOY_URLS`) no arquivo `.env` do DarkHub no Dokploy. |
| **HTTP 400 Invalid Host** | Requisição enviada com cabeçalho `Host` não autorizado. | Adicione o domínio à variável `DARKHUB_ALLOWED_HOSTS` no container (ex: `darkhub.ggcampos.com`). |
| **Downtime durante deploy** | Container anterior derrubado antes do novo subir. | O Dokploy gerencia o rolling update automático (`docker compose up -d --build`). Verifique a diretiva `restart: unless-stopped` no compose. |
