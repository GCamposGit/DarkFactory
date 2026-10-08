# Runbook: progresso do run_ticket na Esteira ao vivo (USR-140 / USR-154)

O `run_ticket.py` publica cada fase (preflight, workspace, agent, gate, commit, pr, ci, merge,
deploy) na tabela append-only `local_run_events` do control store. O DarkHub le essa tabela e mostra a
execucao em `darkhub.ggcampos.com/live`. Sem essa publicacao o ticket aparece como "Fora da esteira".

A publicacao e best-effort: nunca bloqueia nem altera o resultado ou o exit code do run_ticket.

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
| variaveis ... valor mock | a variavel aponta para `mock://...` | apontar para o Postgres real |
| sem permissao de escrita | o usuario do banco nao pode criar a tabela ou inserir | conceder a permissao (secao Postgres) |
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

## Permissao do usuario do Postgres

O usuario da URL precisa, no banco do control store:

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

- `CREATE` no schema e necessario **mesmo com a tabela ja existente**: a primeira publicacao de cada
  processo roda `CREATE TABLE IF NOT EXISTS`, e o Postgres checa `CREATE` no schema antes de ver que a
  tabela existe. Um usuario com so `INSERT` na tabela falha com `InsufficientPrivilege` (o aviso diz
  "sem permissao de escrita"). Sem nenhum `GRANT` no schema, tambem falha.
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

## Verificacao

Depois de configurar, rode um ticket real (`--dry-run` nao publica nada) e confirme em
`darkhub.ggcampos.com/live` que a execucao aparece. Sem o aviso no stderr, a publicacao esta funcionando.
