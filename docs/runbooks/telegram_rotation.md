# Runbook de Rotação de Tokens do Telegram (USR-110)

Este runbook documenta o procedimento completo, seguro e à prova de falhas para rotação e sincronização dos tokens dos bots do Telegram na Dark Factory.

---

## 1. Visão Geral dos Bots e Papéis

A Dark Factory opera com **dois bots distintos** para isolamento de comandos e alertas:

| Bot | Username | Papel | Rota de Webhook no DarkHub |
| :--- | :--- | :--- | :--- |
| **Ops Bot** | `@darkfac_ops_bot` | Interações operacionais, comandos `/linha`, Grill e notas de voz | `/api/webhooks/telegram/ops` |
| **Owner Bot** | `@darkfac_bot` | Alertas críticos, emergências de resiliência e notificações | `/api/webhooks/telegram/owner` |

> [!WARNING]
> Cada bot possui seu próprio token e sua própria rota de webhook. Nunca aponte ambos os bots para a mesma rota ou use o mesmo token para papéis diferentes.

---

## 2. Inventário de Fontes de Token (Onde os tokens residem)

Para evitar divergência de credenciais (onde um arquivo possui token antigo e outro o novo), os tokens são distribuídos nas seguintes camadas:

1. **Servidor Remoto / VPS (Dokploy)**:
   - Aplicação `Darkhub`: Variáveis de ambiente no Dokploy (`TELEGRAM_OPS_BOT_TOKEN`, `TELEGRAM_OWNER_BOT_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`).
   - Aplicação `darkfac-cloud`: Variáveis de ambiente no Dokploy.
2. **Ambiente Local (Notebook / Desktop)**:
   - `C:\dev\DarkFac\.env`: Variáveis `TELEGRAM_OPS_BOT_TOKEN`, `TELEGRAM_OWNER_BOT_TOKEN`, `TELEGRAM_BOT_TOKEN`.
   - `C:\dev\DarkFac\.factory\telegram\owner_config.json`: Configuração local do bot owner (opcional/fallback).
   - `C:\dev\DarkFac\.factory\telegram\ops_config.json`: Configuração local do bot ops (opcional/fallback).
   - `C:\dev\DarkFac\.factory\telegram\config.json`: Configuração legada (deve ser mantida sem token versionado no git).

---

## 3. Passo a Passo de Rotação no Telegram (BotFather)

### Passo 3.1: Obter novos tokens no BotFather
1. No Telegram, abra a conversa com `@BotFather`.
2. Envie o comando `/mybots`.
3. Selecione o bot de operações: `@darkfac_ops_bot`.
4. Clique em **API Token**.
5. Clique em **Revoke current token** para revogar o token comprometido/antigo e gerar o novo token imediatamente.
6. Copie o novo token exibido (formato: `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`).
7. Repita o procedimento para o bot do proprietário: `@darkfac_bot`:
   - `/mybots` -> selecione `@darkfac_bot` -> **API Token** -> **Revoke current token**.
   - Copie o novo token exibido.

---

## 4. Atualização das Fontes de Credencial

### Passo 4.1: Atualização no Dokploy (Produção)
1. Acesse o painel web do Dokploy: `https://dokploy.ggcampos.com`.
2. Navegue até o projeto **darkfac-core** (ambiente **production**).
3. Na aplicação **Darkhub**:
   - Vá na aba **Environment**.
   - Atualize os campos:
     - `TELEGRAM_OPS_BOT_TOKEN`: Cole o novo token do `@darkfac_ops_bot`.
     - `TELEGRAM_BOT_TOKEN`: Cole o mesmo novo token do `@darkfac_ops_bot`.
     - `TELEGRAM_OWNER_BOT_TOKEN`: Cole o novo token do `@darkfac_bot`.
   - Clique em **Save** e **Deploy**.
4. Na aplicação **darkfac-cloud**:
   - Repita a atualização das variáveis correspondentes e redeploye.

### Passo 4.2: Atualização Local no Arquivo `.env`
1. Abra o arquivo `C:\dev\DarkFac\.env` no seu editor.
2. Atualize as chaves com os novos tokens obtidos no BotFather:
   ```env
   TELEGRAM_OPS_BOT_TOKEN=123456789:SEU_NOVO_TOKEN_OPS_AQUI
   TELEGRAM_BOT_TOKEN=123456789:SEU_NOVO_TOKEN_OPS_AQUI
   TELEGRAM_OWNER_BOT_TOKEN=987654321:SEU_NOVO_TOKEN_OWNER_AQUI
   ```
3. Atualize ou limpe os arquivos locais em `.factory/telegram/`:
   - No arquivo `C:\dev\DarkFac\.factory\telegram\ops_config.json`, atualize `"bot_token"` com o token do ops.
   - No arquivo `C:\dev\DarkFac\.factory\telegram\owner_config.json`, atualize `"bot_token"` com o token do owner.

---

## 5. Verificação e Homologação Automatizada

### Passo 5.1: Executar o script de conferência
Execute no PowerShell do Notebook ou Desktop:
```powershell
python C:\dev\DarkFac\scripts\telegram_rotate_check.py
```

O script testará a comunicação real com a API do Telegram (`getMe` e `getWebhookInfo`) para cada fonte de token, exibindo somente fingerprints mascaradas e garantindo que nenhum token seja vazado nos logs.

**Saída esperada de sucesso:**
```text
================================================================================
 VERIFICAÇÃO DE TOKENS DO TELEGRAM (USR-110)
================================================================================

Fonte:       .env [TELEGRAM_OPS_BOT_TOKEN]
Papel:       ops
Fingerprint: 1234...xyz9
getMe:       OK (@darkfac_ops_bot, id=...)
Webhook:     https://darkhub.ggcampos.com/api/webhooks/telegram/ops (pending=0)

Fonte:       .env [TELEGRAM_OWNER_BOT_TOKEN]
Papel:       owner
Fingerprint: 9876...abc1
getMe:       OK (@darkfac_bot, id=...)
Webhook:     https://darkhub.ggcampos.com/api/webhooks/telegram/owner (pending=0)

--------------------------------------------------------------------------------
[PASS] Todos os tokens locais configurados são válidos perante a API do Telegram.
```

### Passo 5.2: Teste E2E no Telegram
1. Abra o Telegram no seu smartphone ou desktop.
2. No chat com `@darkfac_ops_bot`, envie a mensagem: `/start`.
   - O bot deve responder com o menu operacional de boas-vindas da Dark Factory.
3. No chat com `@darkfac_bot`, envie a mensagem: `/start`.
   - O bot deve responder indicando que é o canal exclusivo de alertas e apontar para o `@darkfac_ops_bot`.
