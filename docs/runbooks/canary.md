# Runbook — HF-27-10: Bootstrap do repositorio `darkfac-canary`

Este runbook cobre o que **nenhum agente executa sozinho**: criar o repositorio
`darkfac-canary` de verdade no GitHub, publica-lo no Dokploy em
`canary.<seu-dominio>`, e apontar o DNS. `core/line/canary.py` (HF-27-10) so
funciona de ponta a ponta depois que os passos abaixo forem feitos uma vez;
ate la, rode-o com `--dry-run` ou sem `--base-url` (ele so registra a
telemetria das etapas da linha, sem o smoke check de `/version`).

Todos os passos assumem a versao atual das interfaces em 2026-09. Se um rotulo
de tela nao bater exatamente com o que voce ve, procure o campo mais parecido
pelo nome ou posicao — a estrutura geral (Settings / Environment / Domains)
tende a ser estavel entre versoes.

## Indice

1. Pre-requisito humano: DNS wildcard ou registro `canary`
2. Criar o repositorio minimo `darkfac-canary` (a fabrica gera os arquivos)
3. `gh repo create --private` (publicar no GitHub)
4. Dokploy: criar o app e apontar `canary.<dominio>`
5. Configurar o compose `darkfac-canary` (roda sozinho em loop, sem cron)
6. Verificacao final (aceite do ticket)

---

## 1. Pre-requisito humano — DNS

Se o seu dominio **ja tem** um registro curinga (`*.seu-dominio.com` apontando
para o IP da VPS Dokploy), pule esta secao — `canary.seu-dominio.com` ja
resolve.

Caso contrario, no provedor de DNS do seu dominio (Cloudflare, Registro.br,
Namecheap, etc.):

1. Acesse o painel de DNS do seu dominio.
2. Clique em **Add record** / **Adicionar registro**.
3. Preencha:
   - **Type / Tipo**: `A` (se voce vai apontar direto para o IP da VPS) ou
     `CNAME` (se o Dokploy fornecer um hostname para apontar, como em alguns
     PaaS gerenciados).
   - **Name / Nome**: `canary` (resulta em `canary.seu-dominio.com`).
   - **Value / Conteudo / Target**: o IP publico da sua VPS Hetzner (o mesmo
     usado pelos outros apps Dokploy, ex.: `dokploy.ggcampos.com`) — confira em
     Dokploy > seu app existente > **Domains** para copiar o IP exato.
   - **TTL**: `Auto` ou `3600` (1 hora) se nao houver opcao automatica.
   - **Proxy status** (Cloudflare): comece com **DNS only** (nuvem cinza) para
     evitar problemas de TLS na primeira configuracao; pode ligar o proxy
     depois.
4. Clique em **Save** / **Salvar**.
5. Confirme a propagacao (pode levar de 1 minuto a algumas horas):
   `nslookup canary.seu-dominio.com` deve devolver o IP da VPS.

## 2. Repositorio minimo `darkfac-canary`

A fabrica gera o esqueleto do projeto via `core.adoption.cli init` — isso cria
localmente um diretorio adotado (MISSION.md, AGENTS.md equivalente, etc.) mas
**nao publica nada no GitHub nem faz deploy**. Rode (ajustando o caminho de
destino):

```powershell
python -m core.adoption.cli init C:\dev\darkfac-canary --name darkfac-canary
```

Isso cria `C:\dev\darkfac-canary` com a estrutura minima adotada pela fabrica.
Adicione a esse diretorio um app HTTP minimo com um unico endpoint
`GET /version` que devolva o SHA do commit atual e, depois que o HF-27-10
rodar pela primeira vez, o campo `canary_day` do dia (esse segundo campo e o
que o proprio canario pede como demanda diaria — nao precisa escrever isso
manualmente, so o `/version` inicial com o SHA).

## 3. Publicar no GitHub (`gh repo create --private`)

Com o diretorio de `darkfac-canary` pronto e com pelo menos um commit local:

```powershell
cd C:\dev\darkfac-canary
gh repo create GCamposGit/darkfac-canary --private --source=. --remote=origin --push
```

Se preferir a UI em vez da CLI:

1. Acesse `https://github.com/new`.
2. Preencha:
   - **Owner**: sua organizacao/usuario (ex.: `GCamposGit`).
   - **Repository name**: `darkfac-canary`.
   - **Description** (opcional): `App minimo de canario diario da DarkFac (HF-27-10)`.
   - **Visibility**: marque **Private**.
   - Nao marque "Add a README" (o diretorio local ja tem arquivos).
3. Clique em **Create repository**.
4. Na pagina seguinte, copie os comandos em "…or push an existing repository
   from the command line" e rode-os no diretorio `C:\dev\darkfac-canary`.

## 4. Dokploy — criar o app e apontar `canary.<dominio>`

1. Acesse seu painel Dokploy (ex.: `https://dokploy.ggcampos.com`) e faca
   login.
2. No projeto onde os outros apps DarkFac vivem, clique em **Create
   Application** (ou o botao `+` no canto superior direito).
3. Preencha o formulario de criacao:
   - **Name**: `darkfac-canary`.
   - **App Type / Source Type**: `Application` (Docker/Git), nao "Compose"
     (o app do canario e simples o bastante para nao precisar de compose
     proprio).
   - **Provider**: `GitHub`.
4. Na aba **General** / **Source**, configure:
   - **Repository**: `GCamposGit/darkfac-canary` (autorize o Dokploy GitHub
     App para esse repositorio se solicitado).
   - **Branch**: `main`.
   - **Build Path**: `/` (raiz do repo).
5. Na aba **Build**, escolha o **Build Type** compativel com o app que voce
   escreveu (ex.: `Dockerfile` se voce incluiu um `Dockerfile` minimo, ou
   `Nixpacks`/`Heroku Buildpacks` se preferir deixar o Dokploy detectar
   automaticamente uma app Python/Node simples).
6. Na aba **Environment**, adicione (se seu app precisar):
   - `PORT=8080` (ou a porta que seu app HTTP escuta).
7. Na aba **Domains**:
   - Clique em **Add Domain**.
   - **Host**: `canary.seu-dominio.com` (o mesmo registro criado no passo 1).
   - **Path**: `/` (padrao).
   - **Container Port**: a porta do seu app (ex.: `8080`).
   - **HTTPS**: ligue e escolha **Let's Encrypt** para gerar o certificado
     automaticamente.
   - Clique em **Save**.
8. Clique em **Deploy** (canto superior direito) e acompanhe os logs de build
   ate aparecer "Deployment successful" / status verde.
9. Teste no navegador ou `curl https://canary.seu-dominio.com/version` — deve
   responder com o SHA do commit.

## 5. Configurar o compose `darkfac-canary` (roda sozinho em loop, sem cron)

**Fechamento HF-03-08 / HF-27-10 (2026-09-28): o servico `darkfac-canary` em
`deploy/dokploy/docker-compose.cloud.yml` nao fica mais atras de
`profiles: ["canary"]` nem precisa de agendamento manual.** Ele agora sobe
junto com o resto do stack (`restart: unless-stopped`) e roda em loop dentro
do proprio processo, via `python -m core.line.canary run --base-url
${DARKFAC_CANARY_BASE_URL:-} --every-seconds 900` — o loop e implementado em
`core.line.canary.run_loop` (submete/observa, loga, dorme 15 min, repete; uma
excecao numa iteracao nunca derruba as seguintes). **Nao ha mais um passo
manual de cron/Scheduled Task para o dia-a-dia** — o que resta e so
provisionar as variaveis de ambiente do servico, depois que
`canary.seu-dominio.com` responder (passos 2-4):

1. No ambiente do servico `darkfac-canary` (Dokploy > app > Environment, ou
   `.env` local, conforme `deploy/dokploy/env.cloud.example`), defina:
   - `DARKFAC_CANARY_BASE_URL=https://canary.seu-dominio.com`
   - `DARKFAC_HF02_DATABASE_URL=<mesma URL do coordinator/worker>` — **sem
     isso o canario ainda funciona, mas cai para um SQLite local dentro do
     container** (`core.line.store_selection`, review item 2 do PR #37);
     ele so entra na fila real que o worker le quando essa var aponta para o
     mesmo Postgres do coordinator/worker.
   - `TELEGRAM_OWNER_BOT_TOKEN`, `TELEGRAM_AUTHORIZED_USERS`,
     `TELEGRAM_AUTHORIZED_CHATS` — sem isso o canario ainda escreve o
     relatorio JSON normalmente, mas a notificacao de falha e o resumo
     semanal silenciosamente nao saem (apenas um `logger.warning` local).
     Veja a secao "Owner notifications" em `env.cloud.example` para como
     obter o token/ids pelo @BotFather.
2. Confirme que o volume `darkfac-canary-reports-v2` existe (Dokploy cria
   automaticamente a partir do compose na primeira vez que o servico sobe;
   sem ele, o container perderia os relatorios anteriores a cada restart e o
   green streak nunca passaria de 1 — review item 3 do PR #37). Em Dokploy:
   **Volumes** (menu lateral) deve listar `darkfac-canary-reports-v2` apos o
   primeiro deploy.
3. Para validar manualmente (fora do loop continuo, uma unica iteracao):
   ```powershell
   docker compose -f deploy/dokploy/docker-compose.cloud.yml run --rm darkfac-canary python -m core.line.canary run --base-url $env:DARKFAC_CANARY_BASE_URL
   ```
4. Depois que o compose for implantado pelo Dokploy com as variaveis do
   passo 1, o servico `darkfac-canary` ja fica de pe sozinho e o loop cuida
   da cadencia — nao ha um passo 4 de agendamento para configurar.
5. Dogfood (opcional, so depois de validar o canario por si so): duas formas
   de ligar, ambas desligadas por padrao (`DARKFAC_DOGFOOD_ENABLED=false`):
   - **Embutido no canario**: defina `DARKFAC_DOGFOOD_ENABLED=true` no
     ambiente do servico `darkfac-canary` — ao final de todo `canary run`
     que resultar em `passed`, ele chama `core.line.dogfood` automaticamente
     (ainda sujeito aos gates: streak >= 7, nada em andamento, item nao
     protegido pelo `guard.py`).
   - **Cron proprio**: mantenha `DARKFAC_DOGFOOD_ENABLED=false` no canario e
     crie um segundo Scheduled Task/Cron Job apontando para
     `python -m core.line.dogfood run --force` (o `--force` ignora apenas o
     env-gate da CLI; os gates de streak/in-flight/guard continuam valendo).
     Isso separa a cadencia do canario (pode ser horaria) da do dogfood
     (ex.: uma vez por dia), sem duplicar submissoes -- `submit_dogfood_item`
     tambem e idempotente por `roadmap_item_id`.

## 6. Verificacao final (aceite do ticket)

- [ ] `nslookup canary.seu-dominio.com` resolve para o IP da VPS.
- [ ] `curl https://canary.seu-dominio.com/version` responde 200 com o SHA.
- [ ] `docker compose -f deploy/dokploy/docker-compose.cloud.yml run --rm darkfac-canary python -m core.line.canary run --base-url $env:DARKFAC_CANARY_BASE_URL`
      roda sem erro e escreve `.factory/reports/canary/<hoje>.json` (dentro
      do volume `darkfac-canary-reports-v2`, nao perdido entre execucoes/restarts).
- [ ] Rodar o comando acima duas vezes seguidas no mesmo dia produz o MESMO
      `run_id` no relatorio (idempotente) e o `outcome` avanca de
      `in_progress` para `passed`/`failed` conforme a linha progride --
      nunca `passed=true` num relatorio com etapas pendentes.
- [ ] Apos o deploy do compose, o servico `darkfac-canary` fica `Up` e sem
      `profiles` no `docker compose config` — o loop (`run_loop`,
      `--every-seconds 900`) roda sozinho, sem cron nem Scheduled Task.
- [ ] Com `TELEGRAM_*` configurado, uma falha proposital (rode num dia sem
      `DARKFAC_CANARY_BASE_URL`, por exemplo) chega no Telegram com a etapa
      e o `cause_code`.
- [ ] Depois de 7 dias corridos com relatorio `outcome: "passed"`,
      `python -m core.line.canary status` mostra `"v2_met": true`.

### Retentativa no mesmo dia e dogfood configuravel

- Quando a tentativa do dia termina em falha terminal (`failed`/`timeout`), a
  iteracao seguinte do loop (15 min depois) submete uma **nova tentativa** para o
  mesmo dia: `external_id` `canary:<data>:a2`, `:a3`... (a tentativa 1 mantem o id
  historico `canary:<data>`). Nunca ha duas tentativas em voo, e o limite e
  `DARKFAC_CANARY_MAX_ATTEMPTS_PER_DAY` (padrao 4; valor invalido volta para 4).
- O relatorio do dia (`<data>.json`) lista as tentativas em `attempts`; o dia e
  verde se qualquer tentativa passou (`passed=true`), o que mantem o
  `green_streak` inalterado. O alerta de falha e deduplicado **por tentativa**:
  uma nova tentativa que falha do mesmo jeito alerta uma vez. O resumo semanal
  (segunda-feira) e enviado uma unica vez por dia.
- `DARKFAC_DOGFOOD_MIN_STREAK` (padrao de codigo 7; o compose passa `1`) define
  quantos dias verdes liberam o dogfood. `DARKFAC_DOGFOOD_ENABLED` continua
  `false` por padrao.

## Fora do escopo automatizado (decisao registrada no handoff)

- Nenhum agente executa `gh repo create`, chamadas ao Dokploy, ou alteracoes
  de DNS por voce — sao acoes irreversiveis/externas fora do escopo permitido
  a um agente autonomo (contas, portais, dominios).
- `core/line/canary.py` roda sem erro mesmo sem esses passos: sem
  `--base-url` ele ainda submete a demanda e observa as etapas da linha
  (grill, planning, development, validation, independent_review,
  integration, build_deploy), mas o relatorio do dia fica `outcome: "failed"`
  com `cause_code: "canary_base_url_missing"` -- o smoke check de `/version`
  e obrigatorio para um dia contar como verde (PR #37 review item 1), nunca
  silenciosamente pulado.
