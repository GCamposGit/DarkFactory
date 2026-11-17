# Runbook: progresso do run_ticket e das sessoes interativas na Esteira ao vivo (USR-140 / USR-154 / USR-164)

O `run_ticket.py` publica cada fase (preflight, workspace, agent, gate, commit, pr, ci, merge,
deploy) na tabela append-only `local_run_events` do control store. O DarkHub le essa tabela e mostra a
execucao em `darkhub.ggcampos.com/live`. Sem essa publicacao o ticket aparece como "Fora da esteira".

A publicacao e best-effort: nunca bloqueia nem altera o resultado ou o exit code do run_ticket.

Dois caminhos publicam na mesma tabela e aparecem no mesmo painel, com harness e maquina em cada fase:

1. **`run_ticket.py`** (Notebook, Desktop): automatico, se a maquina tem a URL de escrita (secoes
   "Variavel necessaria" e "Papel de escrita restrito").
2. **Sessoes interativas do Claude Code** (implementacao feita no chat, por subagentes, sem
   `run_ticket.py`): pelo `scripts/live_run.py` (secao "Sessoes interativas").

O cenario que motivou o USR-164: sem URL, o `control.db` local era usado em silencio e o Hub da nuvem
nunca o le, entao o painel mostrava zero runs ativos enquanto o trabalho acontecia. Agora esse caso avisa.

## O aviso

Na primeira vez que o run_ticket nao consegue publicar, ele imprime UM aviso (no maximo um por run,
inclusive no subprocesso de entrega) no **stderr**:

```
[AVISO] Esteira ao vivo: progresso desta execucao nao sera publicado - <motivo>. A execucao continua normalmente. Veja docs/runbooks/live_progress.md.
```

Com `--json` o stdout continua sendo exatamente um documento JSON; o aviso so aparece no stderr.
Credenciais nunca aparecem no aviso (a causa e descrita em palavras, nao com a mensagem do driver).
Sob `pytest` o aviso nunca aparece e nada e publicado.

| Motivo no aviso | Causa | O que fazer |
| --- | --- | --- |
| variavel de banco ausente | nenhuma das variaveis abaixo esta definida e nao ha `control.db` local | definir a variavel (secao seguinte) |
| este run so sera gravado no `control.db` local ... NAO aparecera em darkhub.ggcampos.com/live | nenhuma URL definida, mas existe `control.db` local (so o Hub da nuvem alimenta o `/live`) | definir a URL do Postgres da nuvem; se um Hub local le esse arquivo de proposito, `DARKFAC_LOCAL_PROGRESS=local` |
| variaveis ... valor mock | a variavel aponta para `mock://...` | apontar para o Postgres real |
| sem permissao de escrita | o usuario do banco nao pode inserir em `local_run_events` (ou a tabela nao existe e ele nao pode cria-la) | provisionar o papel restrito (secao seguinte a "Variavel necessaria") |
| banco inalcancavel ou conexao recusada | host/porta/rede (Tailscale), credenciais erradas ou timeout do control store | testar a conexao com `psql` a partir desta maquina |
| driver psycopg nao instalado | falta `psycopg` no Python usado pelo run_ticket | `python -m pip install "psycopg[binary]>=3.2.0,<3.3.0"` (mesma versao das imagens `deploy/dokploy/Dockerfile.cloud` e `Dockerfile.hub`); ou, na raiz do repositorio, `python -m pip install -r requirements-live.txt` |

### Causa estruturada (`cause_code`, USR-455 T2)

"Banco inalcancavel ou conexao recusada" acima e a frase ao operador, mas por tras dela o publicador
classifica a excecao real de uma tentativa de escrita em um codigo fechado
(`core.line.local_progress.classify_connection_cause`), exposto em `ProgressPublisher.warning_cause_code`
e no campo `cause_code` de `publish_status()`/`final_json_payload`:

| `cause_code` | Quando | O que fazer |
| --- | --- | --- |
| `no_url` | nenhuma variavel de banco definida (nem `control.db`) | definir `DARKFAC_HF02_DATABASE_URL` ou `DARKHUB_LINE_DATABASE_URL` |
| `missing_driver` | `psycopg` nao instalado | `python -m pip install "psycopg[binary]>=3.2.0,<3.3.0"` |
| `dns_error` | o host da URL nao resolveu | confira o nome/IP configurado e a tailnet |
| `tcp_timeout` | a rede nao respondeu dentro do tempo configurado | confira firewall/tailnet ou aumente `DARKFAC_LOCAL_PROGRESS_CONNECT_TIMEOUT_S` |
| `connection_refused` | a porta esta fechada ou o servico esta fora do ar | confira o host/porta configurados no banco |
| `auth_failed` | usuario ou senha incorretos na URL | reprovisione com `scripts/provision_live_writer.py` |
| `missing_table` | `local_run_events` ausente e o usuario nao tem `CREATE` | reprovisione com `scripts/provision_live_writer.py` |
| `read_only` | sem permissao de escrita em `local_run_events` | use o papel restrito criado por `scripts/provision_live_writer.py` |
| `unknown` | excecao que nao se encaixa em nenhuma causa acima | veja o tipo de excecao nos logs (`logger.warning` registra `cause=<codigo> <TipoDaExcecao>`) |

Esse `cause_code` classifica uma unica tentativa de conexao/escrita que falhou; ele nao compara o
control store com o sink. Para divergencia de destino (host/porta/base diferentes) ou de orcamento de
timeout entre os dois, use a `causa` offline do `live_progress_doctor` (`dsn_divergente`,
`timeout_curto`, secao seguinte) — codigos distintos, com proposito distinto.

## Variavel necessaria

O control store usa `DARKFAC_HF02_DATABASE_URL`. O sink de progresso prefere a URL de escrita
especifica `DARKHUB_LINE_DATABASE_URL` e usa `DARKFAC_HF02_DATABASE_URL` como fallback. Assim, as duas
URLs podem apontar para o mesmo banco ou para destinos distintos; o sink nao reutiliza por engano uma
URL do control store quando a maquina tem um endpoint proprio para escrita no Hub. O sink usa timeout
de conexao de 10 s por padrao (o control store usa 5 s), evitando que a publicacao expire antes de uma
leitura que conclui no mesmo contexto de rede.

`DARKHUB_CONTROL_DATABASE_URL` e apenas o ultimo fallback e normalmente pertence a um papel de leitura;
prefira uma das duas URLs de escrita acima.

No PowerShell, para o usuario atual (abra um terminal novo depois):

```powershell
[Environment]::SetEnvironmentVariable("DARKFAC_HF02_DATABASE_URL", "<url postgresql do usuario escritor>", "User")
```

A URL tem o formato `postgresql://USUARIO:SENHA@HOST:5432/BANCO`. Nunca a registre em arquivos do repositorio.

Sem nenhuma URL, o run_ticket publica no `control.db` local apenas se o arquivo ja existir
(`state_root()/control.db`); caso contrario, avisa.

Para desligar de proposito (sem aviso): `DARKFAC_LOCAL_PROGRESS=off`.
Para usar o `control.db` local de proposito, porque um Hub rodando nesta maquina le esse arquivo (sem
aviso, ignora as URLs): `DARKFAC_LOCAL_PROGRESS=local`.

### Orcamento de tempo do `PostgresSink` (USR-455 T2)

O `PostgresSink` conecta uma vez por run e reaproveita a mesma conexao em cada fase publicada (nao abre
uma conexao nova por evento). Em falha de conexao reconecta uma unica vez e repete o `INSERT` antes de
propagar o erro ao `ProgressPublisher` (que so conta isso como uma falha apos a tentativa de reconexao).
O sink parte do resolvedor compartilhado em `core/orchestrator/cloud_db.py` (o mesmo usado por
`PostgresControlStore`), mas com limites proprios e deliberadamente mais largos que os 5 s/5000 ms de
conexao/comando do control store -- o controle store so le; o sink publica em paralelo ao run e pode
tolerar mais latencia sem bloquear nada. Cada limite aceita um valor positivo na variavel
correspondente; vazio ou invalido cai no padrao:

| Variavel | Padrao | O que controla |
| --- | --- | --- |
| `DARKFAC_LOCAL_PROGRESS_CONNECT_TIMEOUT_S` | `10` s (max. `30`) | tempo maximo para abrir a conexao |
| `DARKFAC_LOCAL_PROGRESS_STATEMENT_TIMEOUT_S` | `10` s (max. `30`) | limite do Postgres para cada comando |
| `DARKFAC_LOCAL_PROGRESS_WRITE_TIMEOUT_S` | `30` s (max. `120`) | limite de parede de uma publicacao |
| `DARKFAC_LOCAL_PROGRESS_RETRIES` | `1` (max. `2`; `0` desliga) | novas tentativas de escrita apos descartar uma conexao que falhou |

Os limites aceitam somente valores finitos e positivos; valores vazios ou invalidos voltam ao padrao,
e valores acima do maximo sao limitados ao teto. A publicacao pode ser configurada para nao repetir
tentativas, mas nunca faz mais de duas.

## Papel de escrita restrito a `local_run_events` (USR-164)

Cada maquina (Notebook, Desktop) precisa publicar no Postgres da nuvem, mas nao precisa do papel que
escreve em todo o control store. O script abaixo cria um papel que so consegue **inserir** em
`local_run_events` (sem `SELECT`, sem `CREATE` no schema, sem acesso a nenhuma outra tabela) e **audita**
o resultado. E idempotente: pode ser repetido. Nenhuma credencial fica no repositorio.

O `PostgresSink` insere primeiro e so executa o DDL se a tabela nao existir. Por isso o papel restrito
nunca precisa de `CREATE`: o administrador cria a tabela e o indice (o script faz isso) e concede
`INSERT` na tabela e `USAGE` na sequence `local_run_events_event_id_seq` (o script faz isso).

### Passo a passo (uma vez, no Desktop, que ja esta na tailnet)

1. **Descubra a URL de administrador.** E a mesma URL que o `darkfac-coordinator` usa em
   `DARKFAC_HF02_DATABASE_URL` (usuario dono das tabelas), mas com o host da tailnet:
   no Dokploy (`https://dokploy.ggcampos.com`) abra o projeto `darkfac-core` > compose
   `darkfac-coordinator` > aba **Environment** e copie o valor de `DARKFAC_HF02_DATABASE_URL`. Troque o
   host `darkfaccore-postgresprimary-aebh67` por `100.83.176.60` (veja `docs/runbooks/HF-27-09_topology.md`,
   secao 2). O formato final e `postgresql://USUARIO:SENHA@100.83.176.60:5432/BANCO`.
2. **Abra um PowerShell** (menu Iniciar > digite `PowerShell` > Enter) e defina a URL **so nesta
   janela** (nao use `setx`; ela some ao fechar a janela):

   ```powershell
   $env:DARKFAC_ADMIN_DATABASE_URL = "postgresql://USUARIO:SENHA@100.83.176.60:5432/BANCO"
   ```

3. **Veja o SQL antes de executar** (opcional; nao conecta e esconde a senha):

   ```powershell
   python C:\dev\DarkFac\scripts\provision_live_writer.py --print-sql
   ```

4. **Execute.** Valores recomendados: papel `darkfac_live_writer` (padrao) e, para o Hub enxergar os
   eventos, o papel de leitura do Hub em `--reader-role` (o recomendado em `docs/DARKHUB_ROADMAP.md` e
   `darkhub_ro`; se o Hub usa a URL completa do coordinator, omita `--reader-role`):

   ```powershell
   python C:\dev\DarkFac\scripts\provision_live_writer.py --reader-role darkhub_ro
   ```

   Saida esperada no terminal: varias linhas `[ok] ...`, depois `[ok] auditoria: ...` e **uma linha**
   `DARKFAC_HF02_DATABASE_URL=postgresql://darkfac_live_writer:...@100.83.176.60:5432/BANCO`.
   Essa linha contem a senha gerada e **nao sera mostrada de novo**: copie-a agora. Se o script falhar com
   `auditoria de privilegios reprovada`, leia os itens `-` e corrija (o caso comum no Postgres 14 ou
   anterior e `REVOKE CREATE ON SCHEMA public FROM PUBLIC;`) e rode de novo.
5. **Grave a URL do papel restrito em cada maquina que roda tickets** (Desktop e Notebook). No PowerShell
   dessa maquina, cole a URL (a parte depois de `DARKFAC_HF02_DATABASE_URL=`) no lugar de `<url>`; depois
   feche e abra o terminal:

   ```powershell
   [Environment]::SetEnvironmentVariable("DARKFAC_HF02_DATABASE_URL", "<url>", "User")
   ```

   Se a maquina ja tem `DARKFAC_HF02_DATABASE_URL` com outro valor para outro fim (por exemplo o worker
   on-prem), use `DARKHUB_LINE_DATABASE_URL` para o sink de progresso; essa URL tem prioridade para a
   publicacao e nao altera o destino do control store.
6. **Limpe a URL de administrador** da janela do passo 2: feche o PowerShell (ou rode
   `Remove-Item Env:DARKFAC_ADMIN_DATABASE_URL`).
7. **Verifique** (secao "Verificacao" abaixo).

Para trocar a senha do papel depois: repita o passo 4 com `--rotate-password` (ele imprime a nova URL) e
repita o passo 5 em cada maquina. Sem a flag, repetir o script mantem a senha e nao imprime URL.

O que o script concede (e nada alem disso): `CONNECT` no banco, `USAGE` no schema `public`, `INSERT` em
`local_run_events`, `USAGE` na sequence `local_run_events_event_id_seq` e, se pedido, `SELECT` na tabela
para o papel de leitura. A auditoria confirma pelo catalogo (`has_*_privilege`) que o papel nao tem
`CREATE` no schema, nem qualquer privilegio em outra tabela, nem e superuser.

Verificado contra PostgreSQL 16.15 real em 2026-10-09.

## Permissao do usuario do Postgres

O caminho recomendado e o papel restrito da secao anterior. Esta secao descreve o papel de escrita
amplo (que tambem cria a tabela sozinho) e os fatos verificados. O usuario da URL precisa, no banco do
control store:

- `CONNECT` no banco e `USAGE` no schema `public`;
- `CREATE` no schema `public` (a primeira publicacao executa `CREATE TABLE IF NOT EXISTS local_run_events`
  e o indice `idx_local_run_events_run`);
- `INSERT` em `local_run_events`.

Exemplo executado por um administrador (troque `escritor` pelo usuario real):

```sql
GRANT CONNECT ON DATABASE <banco> TO escritor;
GRANT USAGE, CREATE ON SCHEMA public TO escritor;
```

O papel somente leitura do DarkHub precisa apenas de `SELECT` em `local_run_events`.

### GRANT minimo verificado contra Postgres real (USR-167)

Verificado em PostgreSQL 16.15 real (`tests/test_postgres_real_integration.py`, marker
`postgres_integration`, usando um papel `NOSUPERUSER NOCREATEDB NOCREATEROLE`). Os dois `GRANT` acima
bastam para o papel escritor: ele cria e passa a ser dono de `local_run_events`, do indice e da
sequence `local_run_events_event_id_seq` (por isso nao precisa de `GRANT` extra em tabela ou sequence),
e o `add_job_evidence` funciona nas tabelas do control store que o proprio escritor criou.

Fatos verificados que mudam o que se espera do `GRANT`:

- Ate o USR-164 o `CREATE` no schema era necessario **mesmo com a tabela ja existente**: a primeira
  publicacao de cada processo rodava `CREATE TABLE IF NOT EXISTS`, e o Postgres checa `CREATE` no schema
  antes de ver que a tabela existe. Desde o USR-164 o `PostgresSink` insere primeiro e so roda o DDL
  quando a tabela nao existe (`UndefinedTable`), entao um usuario com so `INSERT` na tabela publica sem
  `CREATE` (e o papel restrito da secao anterior). Sem nenhum `GRANT` no schema, ainda falha com
  `InsufficientPrivilege` (o aviso diz "sem permissao de escrita").
- Se um administrador criar a tabela antes (o escritor nao e o dono), alem de `CREATE` no schema o
  escritor precisa de `INSERT` na tabela e `USAGE` na sequence:
  `GRANT INSERT ON local_run_events TO escritor; GRANT USAGE ON SEQUENCE local_run_events_event_id_seq TO escritor;`
- `add_job_evidence` (marcador de orfao, USR-155) executa so `UPDATE` e `SELECT` em `jobs`: com apenas
  `SELECT` falha (`InsufficientPrivilege`, reportado como `StoreUnavailableError`); com `SELECT, UPDATE`
  funciona e e idempotente. O bootstrap do `PostgresControlStore` (`CREATE TABLE IF NOT EXISTS`) exige o
  `CREATE` no schema acima.
- O papel de leitura do Hub le a Esteira com `GRANT CONNECT ON DATABASE <banco> TO leitor; GRANT USAGE ON SCHEMA public TO leitor; GRANT SELECT ON runs, jobs, claims, intake_commands, local_run_events TO leitor;`
  Como `local_run_events` pertence ao escritor, o `GRANT SELECT` nela deve ser feito por um administrador
  depois da primeira publicacao (a tabela so existe a partir dela).

Para repetir a verificacao contra um Postgres descartavel (nunca a producao):

```powershell
docker run -d --name pg-usr167 -e POSTGRES_PASSWORD=<senha-de-teste> -p 127.0.0.1:55432:5432 postgres:16
$env:DARKFAC_TEST_POSTGRES_DSN = "postgresql://postgres:<senha-de-teste>@127.0.0.1:55432/postgres"
python -m pytest C:\dev\DarkFac\tests\test_postgres_real_integration.py -q -p no:cacheprovider
docker rm -f pg-usr167
```

O DSN precisa de um usuario que possa `CREATE ROLE` e `CREATE DATABASE` (cada teste cria e remove seu
proprio banco e papeis). Sem `DARKFAC_TEST_POSTGRES_DSN` os testes sao pulados e a suite padrao e o CI
nao dependem de Postgres.

## Sessoes interativas do Claude Code (USR-164)

Quando o ticket e implementado no chat (subagentes em worktrees, `git`/`gh` a mao, skill
`19-run-ticket`), nao ha `run_ticket.py` para publicar as fases. A sessao chama o CLI fino abaixo, que usa
o mesmo `ProgressPublisher`, a mesma tabela e a mesma resolucao de URL (variaveis desta runbook) e grava
o harness e a maquina em cada fase. Um run por ticket:

```powershell
python C:\dev\DarkFac\scripts\live_run.py open --ticket USR-XX --title "Titulo do ticket" --harness claude
python C:\dev\DarkFac\scripts\live_run.py phase workspace --ticket USR-XX --message "worktree propria"
python C:\dev\DarkFac\scripts\live_run.py phase agent --ticket USR-XX --message "subagente sonnet implementando"
python C:\dev\DarkFac\scripts\live_run.py phase gate --ticket USR-XX --message "portao"
python C:\dev\DarkFac\scripts\live_run.py phase commit --ticket USR-XX
python C:\dev\DarkFac\scripts\live_run.py phase pr --ticket USR-XX --message "PR #123"
python C:\dev\DarkFac\scripts\live_run.py phase ci --ticket USR-XX
python C:\dev\DarkFac\scripts\live_run.py phase merge --ticket USR-XX
python C:\dev\DarkFac\scripts\live_run.py phase deploy --ticket USR-XX
python C:\dev\DarkFac\scripts\live_run.py finish --ticket USR-XX --message "entregue (PR #123)"
```

Fases validas: `preflight`, `workspace`, `agent`, `gate`, `commit`, `pr`, `ci`, `merge`, `deploy`.

Regras do comando:

- `open` abre o run (fase `preflight` em andamento); repetir reaproveita o run existente. O harness vem
  de `--harness`, de `DARKFAC_OPERATING_HARNESS` ou e `claude`; a maquina e o nome do host (`--worker`).
- `phase NOME` inicia a fase e encerra a anterior como bem-sucedida. `--status succeeded|failed|skipped`
  fecha a fase explicitamente (use `--cause gate_red`, `--cause ci_red` etc. nas falhas). Repetir a mesma
  fase/estado/mensagem nao publica nada; mudar a `--message` publica uma atualizacao de andamento.
- `finish` fecha a fase em andamento e o run; com `--failed` marca falha (`--cause`, `--message`).
  `cancel` registra fase e run como cancelados (`owner_cancelled`). Sem run aberto, ambos nao fazem nada.
- **`finish`/`cancel` que nao publicou mantem o estado (USR-202).** Sem URL de escrita, com so o `control.db`
  local, banco inalcancavel ou escrita parcial, o estado `.factory/live_runs/USR-XX.json` NAO e apagado e o
  `[AVISO]` diz isso (`state_kept` no JSON). Corrija a URL e rode o mesmo `finish` (ou `cancel`) de novo: ele
  republica o encerramento do MESMO run (repetir o evento e inofensivo). So apos publicar o estado e apagado.
  Com `DARKFAC_LOCAL_PROGRESS=local` ou `off` (de proposito) o estado e apagado normalmente.
- `phase` sem `open` previo abre o run implicitamente.
- **Nunca derruba a sessao.** URL ausente, banco inalcancavel, diretorio de estado sem escrita ou arquivo
  de estado corrompido geram no maximo UM `[AVISO]` por run no stderr e exit code `0`. Somente comando
  malformado (fase inexistente, falta `--ticket`) sai com `2`. Com `--json`, o stdout e um documento JSON
  (`ok`, `run_id`, `published`, `state_kept`, `warning`, `detail`).
- O estado do run fica em `.factory/live_runs/USR-XX.json` (em `DARKFAC_STATE_ROOT` quando definido,
  ignorado pelo git). Um run sem evento ha mais de 6 h e dado como abandonado e o proximo `open` abre outro.

Se a sessao terminar sem `finish`, o painel marca o run como abandonado apos 6 h sem eventos.

## Diagnostico offline do control store e do sink (USR-455 T1)

Rode o doctor antes de investigar um aviso de progresso do launcher, especialmente quando reservas ou
claims funcionaram no control store, mas uma fase nao apareceu no Hub. Os dois caminhos resolvem seus
destinos independentemente: o control store usa somente `DARKFAC_HF02_DATABASE_URL` (ou `control.db`
local), enquanto o sink prefere `DARKHUB_LINE_DATABASE_URL`, depois `DARKFAC_HF02_DATABASE_URL` e,
por ultimo, `DARKHUB_CONTROL_DATABASE_URL`.

```powershell
python C:\dev\DarkFac\scripts\live_progress_doctor.py
python C:\dev\DarkFac\scripts\live_progress_doctor.py --json
```

O comando nao acessa rede, DNS, psycopg ou SQLite. Ele compara a configuracao local e mostra, para cada
alvo, host, porta, `sslmode`, timeout de conexao e se as credenciais estao presentes. Usuario, senha e
DSN nunca sao exibidos. `sslmode` usa o parametro da URL, depois `PGSSLMODE`, e por fim o padrao libpq
`prefer`. Portas Postgres omitidas aparecem como `5432`; o control store usa timeout de 5 s e o sink
usa `DARKFAC_LOCAL_PROGRESS_CONNECT_TIMEOUT_S` (padrao 10 s).

A linha `causa` classifica a primeira divergencia: `dsn_divergente` para host, porta, base ou `sslmode`
diferentes; `timeout_curto` quando o sink tem timeout menor; `credencial_ausente` quando falta usuario
ou senha; `no_url` quando nao ha URL de escrita nem `control.db` local; `host_sem_rota` quando o host e
malformado ou a configuracao nao pode ser resolvida; e `ok` quando a configuracao e coerente ou a
publicacao esta desligada de proposito. Como o doctor e offline,
`host_sem_rota` nao significa que uma rota real foi testada: DNS e conectividade nao sao verificados.

Codigo de saida `0` significa `ok`; `1` indica configuracao degradada. O JSON contem os mesmos campos
redigidos e o codigo da causa. `ok` nao confirma conectividade, permissao de escrita ou presenca da
tabela. Depois de corrigir a configuracao, confirme a publicacao com um run real ou pelas evidencias de
Postgres descritas abaixo.

## Resultado da publicacao na saida do run_ticket (USR-455 T3)

Toda execucao do `run_ticket.py` (fluxo completo e `--resume-delivery` isolado) e toda chamada do
`scripts/live_run.py` (`open`, `phase`, `finish`, `cancel`) imprime, sempre no **stderr** e sempre,
com sucesso ou falha da publicacao, uma ultima linha:

```
[ESTEIRA] progresso publicado no Hub (darkhub.ggcampos.com/live).
```

ou, quando nenhuma fase chegou a um sink que o Hub da nuvem le (sem URL, `control.db` local,
banco inalcancavel, sem permissao, ou `DARKFAC_LOCAL_PROGRESS=off`):

```
[ESTEIRA] progresso mudo - <motivo curto, sem senha e sem URL>
```

O operador ve esse veredito sem abrir nenhum log: o motivo curto repete a causa do `[AVISO] Esteira ao
vivo` ja impresso no momento da falha (nunca a sentenca completa de novo). Publicacao mudo nao altera
o exit code da execucao. Com `--json` essa linha continua so no stderr; o stdout permanece um unico
documento JSON, que tambem carrega o mesmo veredito em `live_progress`
(`{"published": bool, "warning": str | null, "cause_code": str | null}`). `cause_code` e o da tabela
da secao "Causa estruturada". A linha e o JSON nao trazem senha nem URL de banco.

## Verificacao

Depois de configurar, rode um ticket real (`--dry-run` nao publica nada) e confirme em
`darkhub.ggcampos.com/live` que a execucao aparece. A linha `[ESTEIRA] progresso publicado no Hub` (ou
o campo `live_progress.published: true` no JSON) no stderr confirma a publicacao sem precisar abrir o
painel; `mudo` com o motivo diz exatamente o que corrigir (tabela da secao "O aviso").

Para uma sessao interativa, sem executar nada de verdade:

```powershell
python C:\dev\DarkFac\scripts\live_run.py open --ticket USR-TESTE --title "Teste da esteira" --harness claude
python C:\dev\DarkFac\scripts\live_run.py finish --ticket USR-TESTE --message "teste concluido"
```

Entre o `open` e o `finish` (ou logo depois, o run fica visivel por 24 h) o `/live` deve mostrar o run
`USR-TESTE` em ate 5 s, com o harness `claude` e o nome desta maquina. A linha
`[ESTEIRA] progresso publicado no Hub` no stderr (e a saida `(publicado)` no texto do `[live_run]`),
sem `[AVISO]`, confirma que a URL de escrita esta correta. `mudo` traz o motivo curto e o `cause_code`
no JSON; o exit code do `live_run` continua 0.

### Prova de ponta a ponta contra Postgres real (USR-455 T3)

`tests/test_postgres_real_integration.py::test_hub_read_only_role_reads_local_run_events_into_the_live_line`
publica uma sequencia real de fases (`preflight` -> `succeeded`, `workspace` -> `succeeded`, `agent` ->
`running`) com o `ProgressPublisher` contra um Postgres descartavel, confirma `publisher.warning is None`
e `publisher.hub_visible is True`, depois le de volta com o mesmo leitor da esteira ao vivo do Hub
(`core.workflow.line_live.read_line_live`) e confere `run_id`, `ticket_id` e o status de cada fase
(`LiveStage.status`) um a um. Roda so com `DARKFAC_TEST_POSTGRES_DSN` configurado (secao
"GRANT minimo verificado..." acima); sem a variavel o teste e pulado com uma mensagem fixa, sem URL --
o portao oficial continua verde (o salto conta como um teste executado, nao como ausencia de suite).

### Evidencia sem um Postgres descartavel disponivel (USR-455 T4)

Nem toda maquina (ou execucao headless) tem um Postgres descartavel para `DARKFAC_TEST_POSTGRES_DSN`.
Para confirmar que o control store e o sink desta maquina ficam coerentes sem arriscar escrever em
producao, rode so o diagnostico offline contra a variavel ja configurada (nunca abre conexao):

```powershell
python C:\dev\DarkFac\scripts\live_progress_doctor.py --json
```

`cause: "ok"` confirma que o control store e o sink resolvem o mesmo host, porta, base e `sslmode`,
sem vazar usuario, senha ou URL. Isso nao substitui a prova de ponta a ponta acima (nao confirma
conectividade nem permissao de escrita), mas e a evidencia segura disponivel quando nenhum Postgres
descartavel existe nesta maquina. Nunca use `DARKFAC_TEST_POSTGRES_DSN` apontando para o banco de
producao: a fixture `scratch` cria e derruba bancos e papeis de verdade.

## Cancelar um run_ticket em andamento (USR-166)

O cancelamento usa o mesmo mecanismo do cancelamento de runs da linha (USR-152): ao ser cancelado, o
launcher mata a arvore de processos do agente (e do portao oficial) e nenhum `git commit`, `git push`,
`gh pr create` ou `gh pr merge` e executado depois. A Esteira ao vivo mostra a fase em andamento e o run
como `Cancelada` (causa `owner_cancelled`). O exit code do launcher e `130`.

Por arquivo de controle (portatil, funciona no Windows):

```powershell
python C:\dev\DarkFac\run_ticket.py --cancel USR-XX
```

O comando grava `.factory/local_cancel/USR-XX.cancel` (em `DARKFAC_STATE_ROOT` quando definido); o
launcher consulta o arquivo a cada ~1 s e o consome ao encerrar. Um pedido gravado antes do launcher
iniciar e descartado como obsoleto. Tambem encerra o run: Ctrl+C, `SIGTERM` ou `SIGBREAK` entregues ao
processo do launcher (um segundo sinal volta ao comportamento padrao e interrompe de imediato).

A worktree do ticket e preservada quando o agente ja alterou arquivos; para retomar depois, use
`python C:\dev\DarkFac\run_ticket.py --resume-delivery USR-XX`.
