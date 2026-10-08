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

## Verificacao

Depois de configurar, rode um ticket real (`--dry-run` nao publica nada) e confirme em
`darkhub.ggcampos.com/live` que a execucao aparece. Sem o aviso no stderr, a publicacao esta funcionando.
