# Runbook — HF-27-09: Topologia VPS + on-prem

Este runbook cobre os pre-requisitos humanos (dashboards, portais, arquivos de
ambiente) para colocar em pe a topologia de tres workers descrita em
`docs/handoffs/production-line/HF-27-09.md` e
`docs/PRODUCTION_LINE_PLAN_2026-09-22.md` (secao 7). A fabrica automatiza tudo
o que da para automatizar (detectar, sondar, gerar comando, verificar); os
itens abaixo exigem um clique humano em uma UI de terceiros.

Todos os passos assumem a versao atual das interfaces em 2026-09. Se um rotulo
de tela nao bater exatamente com o que voce ve, procure o campo mais parecido
pelo nome ou posicao descritos e continue — a estrutura geral (Settings /
Environment / Keys) tende a ser estavel entre versoes.

## Indice

1. Tailscale: chaves de auth para VPS e hosts on-prem
2. Postgres: bind exclusivo na interface Tailscale
3. Dokploy: variaveis de ambiente e secrets do worker VPS
4. Claude Code na VPS: `claude setup-token`
5. Codex na VPS: device-auth via Telegram
6. Colocar os workers on-prem de pe (Desktop / Notebook)
7. Verificacao final (aceite do ticket)

---

## 1. Tailscale — gerar auth keys

Precisamos de uma tailnet unindo: VPS (Hetzner CX23), Desktop, Notebook, e o
Postgres (rodando na VPS ou em outro host da tailnet).

1. Acesse `https://login.tailscale.com/admin/machines` e faca login com a
   conta dona da tailnet da fabrica.
2. No menu lateral esquerdo, clique em **Settings**.
3. Clique na aba **Keys**.
4. Clique no botao **Generate auth key...** (canto superior direito).
5. Na caixa de dialogo "Generate auth key", preencha:
   - **Description**: `darkfac-vps-worker` (para a chave da VPS) ou
     `darkfac-onprem-<nome-da-maquina>` para cada host on-prem.
   - **Reusable**: marque OFF (chave de uso unico) a menos que va reinstalar o
     mesmo host varias vezes; nesse caso marque ON.
   - **Ephemeral**: deixe OFF (queremos que o host permaneca registrado na
     tailnet mesmo se ficar offline por um tempo).
   - **Tags**: se sua tailnet usa ACL tags, adicione `tag:darkfac-worker` (ou
     deixe em branco se voce nao configurou tags ainda).
   - **Expiration**: escolha `90 days` (o padrao) e coloque um lembrete para
     renovar; ou `Never expire` se preferir nao repetir este passo (menos
     seguro).
6. Clique em **Generate key**. Copie o valor exibido (comeca com `tskey-auth-`)
   — ele so aparece uma vez.
7. Repita os passos 4-6 para cada host que precisa entrar na tailnet: VPS,
   Desktop, Notebook (uma chave por host, ou uma reusable se preferir).
8. Em cada host, instale o Tailscale (se ainda nao instalado) e rode:
   - VPS (dentro do container ou no host, conforme onde o Postgres/worker
     rodar): `tailscale up --authkey=<chave-gerada> --hostname=darkfac-vps`
   - Desktop: `tailscale up --authkey=<chave-gerada> --hostname=darkfac-desktop`
   - Notebook: `tailscale up --authkey=<chave-gerada> --hostname=darkfac-notebook`
9. Confirme que os tres hosts aparecem em
   `https://login.tailscale.com/admin/machines` com status "Connected", e
   anote o IP `100.x.y.z` de cada um (voce vai precisar do IP da VPS/Postgres
   no passo 3).

## 2. Postgres — bind exclusivo na interface Tailscale

Objetivo: o Postgres so aceita conexoes vindas da tailnet, nunca da internet
publica.

1. No host onde o Postgres roda (normalmente a VPS, no proprio Dokploy ou em
   um container ao lado):
   - Se for um container Docker/Dokploy: no `docker-compose` do Postgres,
     troque a publicacao de porta de `"5432:5432"` para
     `"100.x.y.z:5432:5432"`, onde `100.x.y.z` e o IP Tailscale da VPS (do
     passo 1.9). Isso faz o Docker so escutar naquela interface.
   - Se for um Postgres nativo (nao containerizado): edite
     `postgresql.conf`, campo `listen_addresses`, e troque `'*'` por
     `'localhost,100.x.y.z'`. Depois edite `pg_hba.conf` e adicione uma linha
     `host  all  all  100.64.0.0/10  scram-sha-256` (a faixa CGNAT que o
     Tailscale usa), removendo/comentando qualquer linha `0.0.0.0/0`.
2. Reinicie o servico do Postgres.
3. Do Desktop (ja na tailnet), teste:
   `psql "postgresql://<user>:<senha>@100.x.y.z:5432/<database>" -c "select 1;"`.
   Deve conectar. De uma rede fora da tailnet, a mesma tentativa deve dar
   timeout/connection refused.
4. Anote a URL final, ja no formato Tailscale, para usar nos passos 3 e 6:
   `postgresql://<user>:<senha>@100.x.y.z:5432/<database>`.

## 3. Dokploy — variaveis de ambiente e secrets do worker VPS

1. Acesse seu painel Dokploy (ex.: `https://dokploy.ggcampos.com`) e faca
   login.
2. No menu lateral, clique no projeto que contem os servicos
   `darkfac-coordinator` / `darkfac-worker-1` (o mesmo definido em
   `deploy/dokploy/docker-compose.cloud.yml`).
3. Clique no servico **darkfac-worker-1**.
4. Clique na aba **Environment** (as vezes rotulada "Environment Variables").
5. Cole/edite as seguintes variaveis (uma por linha, formato `CHAVE=valor`;
   veja `deploy/dokploy/env.cloud.example` para a lista completa e
   comentarios):
   - `DARKFAC_HF02_DATABASE_URL`: se o Postgres roda **na propria VPS**
     (mesmo Dokploy), mantenha a URL interna que o worker ja usa hoje (rede
     `dokploy-network`); nao troque. A URL Tailscale do passo 2.4 e para os
     hosts on-prem (secao 6). So use a URL Tailscale aqui se o Postgres
     estiver em outra maquina.
   - `DARKFAC_WORKER_CAPS` = `git,gh,node,python,harness:claude,harness:codex`
   - `DARKFAC_WORKER_PRIORITY` = `primary`
   - `DARKFAC_WORKSPACES` = `/workspaces`
   - `DARKFAC_MAX_CONCURRENT_SLOTS` = `1` (CX23 comporta 1 slot de agente;
     veja secao 7 item 7 do plano mestre para o gatilho de upgrade)
   - `CLAUDE_CODE_OAUTH_TOKEN` = deixe em branco por enquanto; preenchido na
     secao 4.
   - Verifique que `OPENROUTER_API_KEY` / `OPENAI_API_KEY` (fallback) ja
     estao presentes de uma configuracao anterior.
6. Se o Dokploy oferecer um toggle "Secret" ou "Mark as secret" ao lado de
   `CLAUDE_CODE_OAUTH_TOKEN`, ative-o (evita que o valor apareca em logs de
   build).
7. Clique em **Save**.
8. Vá até a aba **Advanced** (ou **General**, dependendo da versao) e confirme
   que os dois volumes `darkfac-codex-auth` e `darkfac-workspaces` (definidos
   em `docker-compose.cloud.yml`) aparecem listados; se o Dokploy pedir para
   voce criar volumes manualmente, use exatamente esses nomes.
9. Clique em **Redeploy** (ou **Deploy**) no topo da pagina do servico para
   aplicar as novas variaveis.

## 4. Claude Code na VPS — `claude setup-token`

O Claude Code nao tem um fluxo de device-auth headless; o token de assinatura
precisa ser gerado por um humano em uma maquina com navegador.

1. No **Desktop** (nao na VPS), abra um terminal e rode:
   `claude setup-token`
2. O comando abre o navegador em uma pagina de login da Anthropic/Claude.
   Faca login com a conta de assinatura da fabrica (Claude Pro/Max/Team, a
   que ja usa no Desktop).
3. Apos autorizar, o terminal imprime um token no formato
   `sk-ant-oat01-...`. Copie o valor completo.
4. Volte ao Dokploy (secao 3), servico **darkfac-worker-1** > aba
   **Environment**, e cole o valor copiado no campo `CLAUDE_CODE_OAUTH_TOKEN`.
5. Clique em **Save** e depois em **Redeploy**.
6. Verificacao automatica: apos o redeploy, a fabrica roda `claude -p "ok"`
   dentro do container (via `core.line.auth_bootstrap.probe_claude`,
   disparado no boot do worker). Se o probe falhar, a capability
   `harness:claude` e removida automaticamente das caps publicadas — nenhum
   job vai ser perdido, mas jobs de `harness:claude` deixam de rodar na VPS
   ate o token ser corrigido. Voce pode confirmar manualmente com:
   `docker exec <container-darkfac-worker-1> claude -p "ok" --output-format json`

Observacao do plano mestre: usar a mesma conta Claude simultaneamente no
Desktop e na VPS pode fazer um login invalidar o outro. Se isso acontecer,
repita este passo para gerar um novo token.

## 5. Codex na VPS — device-auth via Telegram

Diferente do Claude, o Codex suporta login por codigo de dispositivo sem
navegador na propria VPS.

1. Garanta que "device code login" (ou "device authorization") esta
   habilitado nas configuracoes de seguranca da conta ChatGPT usada pela
   fabrica: acesse `https://chatgpt.com/`, clique no seu avatar (canto
   inferior/superior esquerdo) > **Settings** > **Security**, e confirme que
   nao ha um bloqueio explicito a logins por codigo de dispositivo (a maioria
   das contas ja permite por padrao; so verifique se ha um toggle
   desabilitando-o).
2. Dispare o bootstrap a partir de qualquer maquina com acesso ao repo/VPS
   (endpoint ops existente, ou diretamente):
   - Via `docker exec` na VPS:
     `docker exec <container-darkfac-worker-1> python -m core.line.auth_bootstrap codex`
   - Ou localmente, se testando fora do container:
     `python -m core.line.auth_bootstrap codex`
3. O comando roda `codex login --device-auth`, extrai a URL e o codigo de
   verificacao da saida do CLI, e envia os dois pelo Telegram (canal `ops`
   configurado em `core.integrations.telegram`) como mensagem do tipo
   `HumanRequest(kind=account)`.
4. No celular/desktop, abra a mensagem do bot do Telegram da fabrica. Ela
   traz:
   - Um link `https://...` — abra-o no navegador.
   - Um codigo no formato `XXXX-XXXX` — digite-o na pagina aberta.
5. Na pagina do navegador, confirme que voce quer autorizar o dispositivo com
   a conta ChatGPT da fabrica.
6. A fabrica sonda `codex login status` logo em seguida (dentro do mesmo
   comando) e imprime um JSON `{"harness": "codex", "ok": true/false, ...}`.
   Se `ok` vier `false`, repita a partir do passo 2 — o codigo anterior pode
   ter expirado.

Observacao do plano mestre: usar a mesma conta ChatGPT em Desktop e VPS pode
rotacionar o refresh token de um dos dois. Se um deles comecar a falhar o
probe (`auth_expired`), rode o bootstrap novamente so para aquele host.

## 6. Colocar os workers on-prem de pe (Desktop / Notebook)

Nao ha configuracao de dashboard aqui — e local ao Windows de cada maquina.

1. Garanta que `DARKFAC_HF02_DATABASE_URL` (a URL Tailscale do passo 2.4) esta
   disponivel para o script. A forma mais simples e defini-la como variavel
   de ambiente do usuario do Windows uma unica vez:
   - Abra **Configuracoes** > **Sistema** > **Sobre** > **Configuracoes
     avancadas do sistema** > aba **Avancado** > botao **Variaveis de
     Ambiente...**.
   - Em "Variaveis de usuario", clique **Novo...**.
   - **Nome da variavel**: `DARKFAC_HF02_DATABASE_URL`
   - **Valor da variavel**: a URL completa do passo 2.4, ex.
     `postgresql://darkfac:<senha>@100.x.y.z:5432/darkfac`
   - Clique **OK** em todas as janelas.
2. Abra um PowerShell **na raiz do repositorio clonado** nesta maquina e rode,
   para o **Desktop** (prioridade secondary, 3 slots, conforme a tabela do
   ticket):
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_onprem_worker.ps1 -Priority secondary -MaxSlots 3
   ```
   Para o **Notebook** (prioridade fallback, 1-2 slots):
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_onprem_worker.ps1 -Priority fallback -MaxSlots 2
   ```
   O script autodetecta `git`, `gh`, `node`, `python`, `claude`, `codex`,
   `grok`, `antigravity` e `nvidia-smi` (para a cap `gpu`) via `Get-Command`;
   nada instalado nao entra na lista de capabilities publicadas.
3. Confirme na saida do console que `Capabilities` lista o que voce espera
   para aquela maquina (ex.: no Desktop deve incluir `harness:grok`,
   `harness:antigravity`, `gpu`, `target:local_service` se estiverem
   instalados).
4. Para deixar o worker de pe apos reboot/logoff, rode **uma vez** (ja existem
   e nao precisam de alteracao):
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_onprem_worker_service.ps1
   ```
   (requer privilegio de Administrador para registrar a tarefa agendada) ou,
   sem privilegio de administrador:
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_onprem_worker_user_startup.ps1
   ```
   Ambos chamam `start_onprem_worker.ps1 -Headless`, que usa os padroes
   `-Priority secondary -MaxSlots 3` — se este host for o Notebook, edite a
   chamada dentro do instalador (`scripts\install_onprem_worker_service.ps1`
   ou `scripts\install_onprem_worker_user_startup.ps1`, variavel `$action`)
   para incluir `-Priority fallback -MaxSlots 2` antes de rodar o instalador.

## 7. Verificacao final (aceite do ticket)

- [ ] `docker exec <container-darkfac-worker-1> sh -c "claude --version && codex --version && gh --version && git --version"`
      roda sem erro na VPS (pendente de execucao — nao ha Docker disponivel
      no ambiente onde este ticket foi implementado; rodar manualmente apos
      o deploy).
- [ ] As tres tabelas do painel/telemetria mostram os tres workers com caps
      distintas (VPS: sem `harness:grok`/`gpu`; Desktop: com todas; Notebook:
      subconjunto do Desktop).
- [ ] Um job com `target:local_service` so e reivindicado pelo Desktop.
- [ ] Com a VPS parada, o Desktop reivindica jobs de `development` em ate
      ~30s (o `ready_age_sec` do worker secondary).
- [ ] Derrubar o token/login de um harness (ex. renomear temporariamente
      `CLAUDE_CODE_OAUTH_TOKEN`) e reiniciar o worker remove `harness:claude`
      das caps publicadas sem que nenhum job de `harness:claude` seja
      reivindicado e falhe.
