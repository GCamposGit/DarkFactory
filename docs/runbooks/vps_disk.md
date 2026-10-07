# Runbook — Disco da VPS (Hetzner CX23, 40 GB, Dokploy + Docker Swarm)

Origem: incidente de 2026-10-06 (disco 100%, painel Dokploy 502 por mais de 1h, DarkHub 404). Este
runbook descreve o que roda sozinho, os limiares, os comandos de emergencia e como verificar.

## 1. Por que acontecia (causa raiz)

- Cada deploy do compose `darkfac-cloud` reconstroi a imagem (varios GB de camadas + cache do BuildKit) e
  o tag `latest` passa para a imagem nova: a anterior fica sem uso. Varios deploys por dia enchem 40 GB em
  horas (no incidente: imagens 31 GB / 233 imagens, so 11 ativas, mais ~22 GB de build cache).
- A limpeza diaria nativa do Dokploy (`Daily Docker Cleanup`) roda **uma vez por dia, 23:50 UTC**, e executa
  `docker container prune`, `docker image prune --all`, `docker builder prune --all` e
  `docker system prune --all` (nunca volumes). Ou seja, ela era eficaz, mas rara demais; e quando o disco
  chega a 100% o proprio Dokploy cai (502) e nao consegue nem rodar o seu cron.
- A USR-122 nunca chegou a atuar: (1) o servico `darkfac-backup-cron` nao recebia `DOKPLOY_API_URL` /
  `DOKPLOY_API_KEY`, entao `clean_vps_if_configured()` retornava `None` em silencio; (2) so rodava apos um
  backup 100% bem-sucedido, uma vez a cada 24h; (3) o alerta de 85% dependia de `scripts/dokploy_redeploy.py`
  ler `disk_percent` de `https://darkhub.ggcampos.com/health`, que so devolve disco com `?disk=true`
  (o valor nunca aparecia, o alerta nunca disparava) e so rodava quando alguem fazia um redeploy.

## 2. O que roda automaticamente agora

| O que | Onde | Quando | Efeito |
|---|---|---|---|
| Disk guard (`core.infra.disk_guard`) | thread do servico `darkfac-backup-cron` | a cada 30 min | le o disco do HOST; poda imagens sem uso (e build cache) se livre < 8 GB; alerta 80%/90% |
| Poda programada de imagens sem uso | idem | a cada 6h (e no start do servico) | `settings.cleanUnusedImages` via API do Dokploy, so se nenhum deploy estiver `running` |
| Retencao de test logs | idem | a cada 6h | apaga arquivos de `/app/.factory/test_logs` com mais de 14 dias (`DARKFAC_TESTLOG_RETENTION_DAYS`) |
| Limpeza pre/pos-deploy | `scripts/dokploy_redeploy.py` | antes (so se livre < 8 GB) e depois de cada redeploy bem-sucedido (imagens sempre; build cache so se livre < 8 GB) | idem; `--skip-disk-hygiene` desliga |
| Limpeza pre/pos-deploy da linha | `core/line/stage_release.py` (estagio `build_deploy`, alvos Dokploy) | antes e depois de cada deploy | mesma politica; nunca altera o resultado do estagio |
| Limpeza de emergencia | `scripts/dokploy_redeploy.py` | quando um build falha com assinatura de disco cheio | poda tudo e manda rodar o redeploy de novo |
| Retencao de workspaces da linha | `CloudWorker` (`core/line/workspace.py::sweep_stale_workspaces`) | a cada 6h | apaga `/workspaces/<projeto>/runs/<run>` de runs terminais ha mais de 3 dias (`DARKFAC_WORKSPACE_RETENTION_DAYS`, 0 desliga); nunca run ativo/com job vivo/em andamento neste worker; orfaos so apos 14 dias |
| Logs de container limitados | `deploy/dokploy/docker-compose.{cloud,hub,n8n}.yml` | sempre | `json-file`, `max-size 10m`, `max-file 3` por servico |
| Limpeza nativa do Dokploy | Dokploy | diario 23:50 UTC | continua como rede de seguranca |

Regras de seguranca da poda (todas verificadas por testes): nunca `--volumes`; nunca para container em
execucao; nunca durante um deploy em andamento (status `running` no Dokploy) — as podas da API do Dokploy
sao `--all` sem filtro de idade e poderiam remover uma imagem recem-construida que ainda nao subiu — exceto
em emergencia (livre <= 2 GB); se a checagem de deploy falhar, nao poda (fail-closed).

## 3. Limiares e alertas

- 80% de uso do disco do host: alerta **AVISO** no bot do owner (Telegram).
- 90%: alerta **CRITICO**.
- No maximo 1 alerta por nivel a cada 6h (estado em `/app/.factory/artifacts/disk_guard_state.json`, no
  volume `darkfac-artifacts`, sobrevive a reinicios). Subir de 80 para 90% alerta na hora.
- A mensagem traz usos, maiores consumidores (`/workspaces`, artefatos, test logs, e o `docker system df`
  do Dokploy quando disponivel), se a limpeza foi adiada (deploy em andamento) e se as credenciais do
  Dokploy estao ausentes (nesse caso diz explicitamente que a limpeza automatica esta DESATIVADA).
- Dentro de um container `/` reporta o filesystem que sustenta `/var/lib/docker`, isto e, o disco da VPS.

## 4. Comandos de emergencia (no host, via SSH; foram os usados no incidente)

```bash
docker system df                       # onde esta o espaco
docker builder prune --all --force     # build cache (liberou 22,5 GB no incidente)
docker image prune --all --force       # imagens sem uso
docker container prune --force         # containers parados (ex.: tasks Swarm Exited (137))
df -h /                                # confirmar
```

**Nunca** `docker volume prune` nem `docker system prune --volumes`: apagam volumes de dados (Postgres,
artefatos, `darkfac-codex-auth`).

## 5. Passos unicos do owner (so uma vez)

### 5.1 Garantir as variaveis do servico `darkfac-backup-cron`

O compose agora injeta `DOKPLOY_API_URL`, `DOKPLOY_API_KEY` e `DARKFAC_VPS_MIN_FREE_GB`. Elas vem do
ambiente do compose `darkfac-cloud` no Dokploy (as mesmas que o worker ja usa).

1. Dokploy > projeto `darkfac-core` > compose `darkfac-cloud` > aba **Environment**.
2. Confirme que existem `DOKPLOY_API_URL=https://dokploy.ggcampos.com` e `DOKPLOY_API_KEY=<chave admin>`.
   Se a chave nao for de um usuario **admin**, gere outra em Settings > Profile > API/CLI > Generate.
3. Salve e faca o deploy normal (a fabrica faz isso ao mergear em `main`).

### 5.2 (Recomendado) Job agendado nativo com filtro de idade

A API do Dokploy so oferece poda `--all` sem filtro de idade. Para uma poda mais fina (imagens sem uso ha
mais de 48h e build cache com mais de 24h, a cada 6h) crie um Schedule do tipo "Dokploy Server":

1. Dokploy > **Settings** > **Schedules** (se a sua versao nao tiver, procure "Schedules" ou "Scheduled Jobs").
2. **Add Schedule**: Name `docker-prune-6h`, Type `Dokploy Server`, Shell `bash`, Cron `0 */6 * * *`.
3. Command:
   `docker image prune --all --force --filter until=48h && docker builder prune --force --filter until=24h && docker container prune --force --filter until=24h`
4. Enabled = ligado, **Create**; use **Run Manually** uma vez e confirme no log que concluiu.

Observacao: este job roda sem checar se ha deploy em andamento; os filtros de idade tornam isso seguro
(uma imagem recem-construida tem menos de 24h).

### 5.3 (Recomendado) Limite de historico de tasks do Swarm

Tasks `Exited (137)` antigas fixam imagens velhas. No host (SSH), uma vez:

```bash
docker swarm update --task-history-limit 1
```

Nao reinicia servicos. O padrao do Docker e 5 por servico.

### 5.4 (Opcional) Limite de log por daemon

Aplica-se tambem aos apps que nao sao compose (ex.: o app `darkfac-canary`). Em `/etc/docker/daemon.json`:

```json
{ "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "3" } }
```

Exige `systemctl restart docker` (reinicia todos os containers por alguns segundos) e so vale para
containers criados depois. Faca em horario calmo.

### 5.5 Logs do Traefik / deploys do Dokploy

- Logs de deploy do Dokploy: o proprio Dokploy mantem so os ultimos 10 por app/compose (nada a fazer).
- Access log do Traefik: Dokploy > Settings > Web Server > **Log Cleanup** (cron semanal) deve estar ligado;
  confirme o toggle.

## 6. Como verificar

```powershell
python C:\dev\DarkFac\scripts\dokploy_redeploy.py --list
```

1. Logs do servico `darkfac-backup-cron` no Dokploy: devem aparecer `Disk guard started: check every 30 min`
   e, a cada ciclo, nenhuma linha `Dokploy credentials missing`.
2. `https://darkhub.ggcampos.com/health?disk=true` devolve `disk_percent`, `disk_free_gb`, `disk_total_gb`.
3. Teste do alerta sem encher o disco: em Environment do `darkfac-cloud` ponha temporariamente
   `DARKFAC_VPS_MIN_FREE_GB=39`; no proximo ciclo (<= 30 min) o guard deve podar imagens/cache (log
   `cleaned`), e o alerta de 80% so dispara de verdade se o disco passar de 80%. Volte para `8` depois.
4. Apos um redeploy, a saida traz `[DISK HYGIENE] pre-deploy: ...` e `[DISK HYGIENE] post-deploy: ...`.
5. Workspaces: `docker exec darkfac-worker-1 du -sh /workspaces` fica estavel ao longo dos dias.

## 7. Limites conhecidos

- `darkfac-artifacts` (artefatos dos workflows) nao tem retencao: apagar pode quebrar evidencia de runs
  antigos. Seu tamanho aparece no alerta; ticket aberto para definir a politica.
- O espelho git por projeto (`/workspaces/<projeto>/.mirror`) cresce lentamente com o repositorio.
- O Postgres de controle fica em outro servico do Dokploy (fora destes composes); acompanhe pelo alerta de disco.
