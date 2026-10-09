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

---

## 6. Conferir todas as fontes de token por papel (USR-197)

O loader (`load_telegram_config`) usa esta precedencia: **variavel de ambiente do processo > `.env` > `.factory/telegram/<papel>_config.json` > `config.json`**. Um token revogado esquecido em uma fonte secundaria so aparece quando a fonte de cima falha, e o aviso `Telegram token sources diverge` do log nao diz qual delas esta revogada. O script abaixo responde isso de uma vez.

### Passo 6.1: Rodar a conferencia completa (com rede)
No PowerShell do Notebook ou do Desktop:
```powershell
python C:\dev\DarkFac\scripts\telegram_tokens_check.py
```

Para cada papel (`owner`, `ops`) e cada fonte (variaveis de ambiente do processo, variaveis do escopo **User** do Windows, `.env`, JSON em `.factory/telegram`), o script chama `getMe` com timeout curto e imprime uma tabela `papel | fonte | final | estado`:

| Estado | Significado |
| :--- | :--- |
| `VALIDO(@bot)` | Telegram aceitou o token (mostra o bot dono dele) |
| `REVOGADO` | Telegram respondeu 401/404: token antigo ou invalido |
| `vazio` | A chave existe mas esta em branco |
| `ausente` | A chave/arquivo nao existe nessa fonte |
| `ERRO(rede)` | Nao foi possivel consultar o Telegram (nao conta como revogado) |
| `FORMATO INVALIDO` | O valor nao tem o formato `numero:segredo` |

A coluna `final` mostra somente os **4 ultimos caracteres**; o token inteiro nunca e impresso. A linha marcada `<- EFETIVA (usada pelo loader)` e a fonte que o bot de fato usa (o script chama o proprio loader). A linha `env(User Windows)` nao e lida pelo loader, mas e herdada por qualquer terminal novo, entao pode virar a fonte efetiva depois de um logoff.

O script avisa quando o valor tem **espacos ou quebra de linha** nas pontas (comum ao colar o token) e sai com **codigo 1** se a fonte efetiva ou qualquer fonte secundaria estiver `REVOGADA`, ou se o papel ficar sem token efetivo.

### Passo 6.2: Limpar uma fonte revogada
1. Anote na tabela qual `fonte` esta `REVOGADO` e qual e a `EFETIVA`.
2. Se for `.env` ou JSON: abra o arquivo indicado (`C:\dev\DarkFac\.env` ou `C:\dev\DarkFac\.factory\telegram\<papel>_config.json`), apague o valor revogado (ou troque pelo token valido da secao 3) e salve.
3. Se for `env(User Windows)`: pressione `Win` + `R`, digite `rundll32 sysdm.cpl,EditEnvironmentVariables`, selecione a variavel em **Variaveis de usuario**, clique em **Excluir** (ou **Editar** e cole o token valido), confirme com **OK** e abra um novo terminal.
4. Rode o script de novo ate sair `OK (codigo 0)`.

### Passo 6.3: Conferir so formato e espacos (sem rede)
```powershell
python C:\dev\DarkFac\scripts\telegram_tokens_check.py --offline
```
Util sem internet: valida apenas formato e espacos nas pontas, sem chamar a API do Telegram.
