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
| banco inalcancavel ou conexao recusada | host/porta/rede (Tailscale), credenciais erradas ou timeout (3 s de conexao) | testar a conexao com `psql` a partir desta maquina |
| driver psycopg nao instalado | falta `psycopg` no Python usado pelo run_ticket | `python -m pip install "psycopg[binary]"` |

## Variavel necessaria

O run_ticket usa a primeira variavel definida, nesta ordem:

1. `DARKFAC_HF02_DATABASE_URL` (recomendada; usuario com permissao de escrita)
2. `DARKHUB_LINE_DATABASE_URL`
3. `DARKHUB_CONTROL_DATABASE_URL` (ultimo recurso: e o papel somente leitura do Hub, normalmente sem escrita)

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
   on-prem), use `DARKHUB_LINE_DATABASE_URL` no lugar: o run_ticket aceita as duas, nessa ordem.
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
- `phase` sem `open` previo abre o run implicitamente.
- **Nunca derruba a sessao.** URL ausente, banco inalcancavel, diretorio de estado sem escrita ou arquivo
  de estado corrompido geram no maximo UM `[AVISO]` por run no stderr e exit code `0`. Somente comando
  malformado (fase inexistente, falta `--ticket`) sai com `2`. Com `--json`, o stdout e um documento JSON
  (`ok`, `run_id`, `published`, `warning`, `detail`).
- O estado do run fica em `.factory/live_runs/USR-XX.json` (em `DARKFAC_STATE_ROOT` quando definido,
  ignorado pelo git). Um run sem evento ha mais de 6 h e dado como abandonado e o proximo `open` abre outro.

Se a sessao terminar sem `finish`, o painel marca o run como abandonado apos 6 h sem eventos.

## Verificacao

Depois de configurar, rode um ticket real (`--dry-run` nao publica nada) e confirme em
`darkhub.ggcampos.com/live` que a execucao aparece. Sem o aviso no stderr, a publicacao esta funcionando.

Para uma sessao interativa, sem executar nada de verdade:

```powershell
python C:\dev\DarkFac\scripts\live_run.py open --ticket USR-TESTE --title "Teste da esteira" --harness claude
python C:\dev\DarkFac\scripts\live_run.py finish --ticket USR-TESTE --message "teste concluido"
```

Entre o `open` e o `finish` (ou logo depois, o run fica visivel por 24 h) o `/live` deve mostrar o run
`USR-TESTE` em ate 5 s, com o harness `claude` e o nome desta maquina. A saida `(publicado)` no terminal,
sem `[AVISO]`, confirma que a URL de escrita esta correta.

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
