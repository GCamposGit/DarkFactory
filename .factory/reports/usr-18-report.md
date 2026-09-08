# Relatório de Execução: USR-18 - DarkHub 24/7 & Gateway de Webhooks Autônomos no Dokploy (INFRA-09 / DF-20/21)

- **Ticket**: `USR-18`
- **Título**: DarkHub 24/7 & Gateway de Webhooks Autônomos no Dokploy (INFRA-09 / DF-20/21)
- **Identificadores Vinculados**: `INFRA-09` (Pipelines de Deploy Contínuo Git Webhooks), `DF-20` (Autonomy & Delivery Policy), `DF-21` (Task Dashboard & Central State Projection)
- **Origem**: `user-demand`
- **Owner**: `antigravity-orchestrator`
- **Data / Horário**: 2026-09-08T18:28:00Z
- **Status do Ticket**: `completed`
- **Workspace**: `C:\dev\DarkFac`

---

## 1. Escopo e Objetivos Atingidos

1. **Conteinerização Dokploy PaaS 24/7 (`deploy/dokploy/Dockerfile.hub` e `docker-compose.hub.yml`)**:
   - Imagem otimizada baseada em `python:3.12-slim` com usuário não-privilegiado `darkfac` (UID 1000).
   - Probes de saúde HTTP periódicos em `/api/cloud/status` (`HEALTHCHECK` nativo).
   - Manifesto Docker Compose para o Dokploy com labels Traefik para SSL automático Let's Encrypt sob o domínio `https://darkhub.ggcampos.com`.
   - Volumes persistentes de dados: `darkhub-factory-data` montado em `/app/.factory` e `darkhub-hub-data` montado em `/app/hub/data`.
   - Template documentado de variáveis de ambiente em `deploy/dokploy/env.hub.example`.

2. **Gateway Autônomo de Webhooks Headless (`hub/backend/webhooks.py`)**:
   - **Autenticação HMAC-SHA256 Timing-Safe**: validação estrita do cabeçalho `X-Hub-Signature-256` utilizando `hmac.compare_digest` para neutralizar ataques de temporização e falsificação de requisições.
   - **Idempotência Anti-Replay**: rastreamento determinístico de `X-GitHub-Delivery` em `.factory/webhooks/deliveries.json` com escrita atômica contra requisições duplicadas.
   - **Dispatcher Autônomo de Eventos**:
     - `ping`: handshake e verificação de conectividade.
     - `push`: detecção de branches, integrando com o cliente de auto-deploy Dokploy (INFRA-09) para disparar publicação na branch `main`.
     - `pull_request` & `check_run`: integração direta com a política determinística `core.orchestrator.delivery.DeliveryPolicy` (DF-20), avaliando conformidade de SHA candidato e enfileirando no `MergeQueue` quando elegível.
     - `workflow_run`: auditoria de execuções remotas.
   - **Trilha de Auditoria**: persistência dos eventos em `.factory/webhooks/events.json`.

3. **Segurança de Borda & Cloud Containment (`hub/backend/main.py`)**:
   - Atualização do `SecurityContainmentMiddleware` para suportar `DARKHUB_ALLOWED_HOSTS` dinâmico via variáveis de ambiente, autorizando `darkhub.ggcampos.com`, `dokploy.ggcampos.com` e IPs de malha Tailscale sem expor o backend a DNS rebinding.
   - Suporte a cabeçalhos de identidade Cloudflare Access Zero Trust (`Cf-Access-Authenticated-User-Email` e `Cf-Access-Jwt-Assertion`) sob a regra inegociável de portas fechadas (Zero Open Ports).

4. **Extensão dos Endpoints REST do DarkHub (`hub/backend/api.py` e `service.py`)**:
   - `POST /api/webhooks/github`: endpoint receptor de webhooks com verificação fail-closed de assinatura e entrega.
   - `GET /api/webhooks/events`: consulta paginada ao histórico de auditoria de webhooks.
   - `POST /api/webhooks/test`: endpoint de simulação determinística para validação em ambientes sem conectividade externa.
   - `GET /api/cloud/status`: diagnóstico de saúde do gateway cloud, domínios autorizados e status do listener.
   - `POST /api/cloud/deploy`: acionador programático do webhook de deploy contínuo do Dokploy (INFRA-09).

5. **Monitoramento na Interface Web do DarkHub (`hub/frontend/infra.js`)**:
   - Card dedicado para "DarkHub 24/7 Gateway & Webhooks Dokploy" montado no painel de infraestrutura.
   - Exibição de modo operacional (Cloud Dokploy 24/7 vs Local Workstation), status da assinatura HMAC, contador de entregas recebidas e badge Cloudflare Zero Trust / Traefik SSL.
   - Botão interativo para simulação instantânea de ping de webhook.

---

## 2. Evidência Determinística de Validação

### Suíte Focal de Reachability
Comando executado:
```powershell
python -m pytest tests/test_darkhub_cloud_gateway.py -v
```
**Resultado**: 14/14 testes aprovados (100% pass) em 0.43s:
- `test_verify_github_signature_valid`: PASSED
- `test_verify_github_signature_tampered_payload`: PASSED
- `test_verify_github_signature_missing_header`: PASSED
- `test_verify_github_signature_fail_closed_when_secret_absent_in_prod`: PASSED
- `test_webhook_idempotency_prevents_replay`: PASSED
- `test_webhook_push_event_dispatching`: PASSED
- `test_webhook_pull_request_df20_policy_evaluation`: PASSED
- `test_allowed_hosts_containment_with_cloud_domain`: PASSED
- `test_cloudflare_zero_trust_access_header_enforcement`: PASSED
- `test_dokploy_deploy_client_skipped_when_empty`: PASSED
- `test_rest_api_webhook_github_endpoint`: PASSED
- `test_rest_api_webhook_events_and_test_simulation`: PASSED
- `test_rest_api_cloud_gateway_status_and_deploy_trigger`: PASSED
- `test_dokploy_manifests_structure`: PASSED

### Validação Obrigatória do Harness
Comando executado:
```powershell
python core/harness/runner.py --quick
```
**Resultado**:
```text
[STEP_PASS] syntax_and_types
[STEP_PASS] unit_and_integration_tests
[TEST_COUNT] count=418
417 passed, 1 skipped in 35.77s
[HARNESS_PASS]
```
Zero regressões introduzidas na fábrica.

---

## 3. Instruções de Implantação no Dokploy PaaS (Guia do Operador)

Para ativar o DarkHub 24/7 no servidor Hetzner CX23 (`darkfac-vps-primary`):

1. **Acessar o Painel Dokploy**:
   - URL: `https://dokploy.ggcampos.com`

2. **Criar Nova Aplicação no Dokploy**:
   - Tipo: **Compose** (ou **Application** via Git Repository).
   - Repositório: `GCamposGit/DarkFactory` (ou upload do `docker-compose.hub.yml`).
   - Caminho do Compose: `deploy/dokploy/docker-compose.hub.yml`.

3. **Configurar Variáveis de Ambiente**:
   Copiar os valores a partir de `deploy/dokploy/env.hub.example`:
   ```bash
   PORT=8000
   DARKHUB_ALLOWED_HOSTS=localhost,127.0.0.1,darkhub.ggcampos.com,dokploy.ggcampos.com,178.105.73.168,100.83.176.60
   GITHUB_WEBHOOK_SECRET=<SEGREDO_GERADO>
   DOKPLOY_DEPLOY_URL=<URL_DO_WEBHOOK_DE_DEPLOY_NO_DOKPLOY>
   CLOUDFLARE_ZERO_TRUST_ENABLED=true
   ```

4. **Configurar Webhook no Repositório GitHub**:
   - Em `Settings > Webhooks > Add webhook`:
     - Payload URL: `https://darkhub.ggcampos.com/api/webhooks/github`
     - Content type: `application/json`
     - Secret: `<SEGREDO_GERADO>`
     - Events: `Pushes`, `Pull requests`, `Workflow runs`, `Check runs`.

5. **Disparar Deploy**:
   - Clicar em **Deploy** no Dokploy. O Traefik irá provisionar o certificado Let's Encrypt para `https://darkhub.ggcampos.com` automaticamente.
