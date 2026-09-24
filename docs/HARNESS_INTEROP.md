# Interoperabilidade entre harnesses

O repositório é deliberadamente independente do editor ou do agente. O contrato comum está em `MISSION.md`, `FACTORY_RULES.md` e `AGENTS.md`.

## Clone em outro PC

```bash
git clone <URL_DO_REPOSITORIO> DarkFac
cd DarkFac
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python core/harness/runner.py --quick
```

`runner.py --quick` já roda a suíte inteira (paralela, cacheada, enfileirada
por máquina — veja "Aceleração da suíte de testes" abaixo). Não é preciso
rodar `pytest` de novo como segundo comando.

O stack de GPU/transcrição é opcional:

```bash
python -m pip install -r requirements-audio.txt
```

## Antigravity

Abra a raiz clonada como workspace. O catálogo `.agents/skills/` é a fonte canônica das 17 skills do projeto.

## Grok

Abra a raiz clonada como workspace e injete `AGENTS.md` como instrução de projeto quando o harness oferecer essa opção. Mesmo sem carregamento automático de skills, o agente pode seguir `FACTORY_RULES.md` e executar o harness determinístico.

## Claude Code

Abra a raiz clonada como workspace. Não há `CLAUDE.md` próprio neste repositório: `AGENTS.md`, `MISSION.md` e `FACTORY_RULES.md` funcionam como o contrato equivalente. O catálogo `.claude/skills/` é o espelho sincronizado de `.agents/skills/` (via `scripts/sync_skills.py`) e é carregado automaticamente. Use o subagente declarativo `.claude/agents/test-runner.md` para delegar execução de testes conforme a skill `17-specialized-test-subagent`.

## DarkHub

```bash
python run_hub.py
```

O serviço fica em `http://127.0.0.1:8888` por padrão. Projetos locais de demonstração não fazem parte do clone compartilhado e continuam disponíveis apenas em seus próprios diretórios.

## Skills em ambientes compatíveis

- Antigravity e Codex: `.agents/skills/`.
- Claude Code: `.claude/skills/`, sincronizado pelo script `python scripts/sync_skills.py`.
- Grok e outros harnesses: use o contrato raiz e, se houver suporte a skills, aponte-o para `.agents/skills/`.

## Aceleração da suíte de testes (lock, cache, xdist)

A fábrica roda em 3 hosts (VPS Linux 2 vCPU via Dokploy, Desktop Windows,
Notebook Windows), todos executando `core/orchestrator/cloud_worker.py`, e
múltiplos harnesses (Claude Code, Codex, Grok, Antigravity) podem validar no
mesmo host ao mesmo tempo. Sem coordenação, isso significa N processos
disputando os mesmos núcleos de CPU e o mesmo `control.db`/`orchestrator.sqlite3`,
todos estourando o timeout de 1200s. Três mecanismos resolvem isso, todos em
`core/harness/`:

1. **Suite lock (`core/harness/suite_lock.py`)** — lock de arquivo por
   máquina (`msvcrt.locking` no Windows, `fcntl.flock` no POSIX; o SO libera
   o lock se o processo morrer, então nunca sobra lock travado). Garante que
   só `DARKFAC_SUITE_SLOTS` (padrão 1) execuções completas da suíte corram
   por vez naquele host. Um segundo processo esperando imprime
   `[SUITE_LOCK] waiting: held by pid ... on ... since ...`. Vale tanto para
   `python core/harness/runner.py --quick` quanto para qualquer `pytest`
   direto na pasta `tests/` (via `tests/conftest.py`) — ou seja, cobre
   qualquer agente/harness, não só o harness oficial.
2. **Verdict cache (`core/harness/cache.py`)** — a chave é um hash sobre a
   *árvore* do commit (`git rev-parse HEAD^{tree}`, não o SHA), o hash da
   config, os nomes dos steps selecionados, as flags `--quick`/`--holdout`,
   a família de plataforma (`windows`/`posix`) e a versão do Python. Um
   rebase ou merge commit que reproduz a mesma árvore reaproveita o PASS
   sem rodar nada. Só verdicts **PASS** são reaproveitados — falha nunca é
   cacheada (evita mascarar flakes). Backend local: JSON no diretório de
   estado da máquina. Backend remoto (opcional): tabela Postgres
   `harness_verdicts`, usada só quando `DARKFAC_HF02_DATABASE_URL` está
   configurada **e** `psycopg` está instalado; qualquer falha remota
   degrada para local-only com um aviso, nunca derruba o harness.
3. **Single-flight cross-host** — quando dois hosts tentam validar a mesma
   árvore ao mesmo tempo, uma tabela Postgres `harness_inflight` garante que
   só um deles execute; o outro faz polling (a cada ~10s, limitado por
   `DARKFAC_HARNESS_REMOTE_WAIT_SEC`) esperando o verdict aparecer em
   `harness_verdicts` em vez de duplicar a execução. Esse polling acontece
   **antes** (e fora) do lock local da máquina — host B nunca fica com sua
   fila de suítes travada enquanto espera o verdict de A; só depois de
   parar de esperar (verdict encontrado, ou ninguém mais segurando a chave)
   é que B tenta o lock local. A reivindicação em `harness_inflight` usa um
   *lease* curto (`DARKFAC_HARNESS_INFLIGHT_LEASE_SEC`, default 90s) em vez
   do tempo total dos steps (~20 min): quem está de fato rodando renova o
   lease a cada `lease/3` numa thread de heartbeat; um processo/container
   que crasha sem chance de liberar a chave só bloqueia por um lease, não
   pelos ~20 min inteiros. Um poll que perceba a linha `harness_inflight`
   ausente ou expirada e nenhum verdict PASS presente para automaticamente
   — o dono terminou (com falha, já que falha nunca é cacheada e o único
   rastro de uma falha é a linha `inflight` sendo apagada) ou crashou; de
   qualquer forma, esperar mais não tem sentido.
4. **Paralelismo (`pytest-xdist`) + timeout por teste (`pytest-timeout`)** —
   o step de teste roda com `-n auto --dist worksteal -m "not serial"`
   (medido no Notebook Windows, i7-14700HX 28 threads: `worksteal` ~120s de média contra
   ~191s de `loadfile` em duas execuções de cada, suíte estável nas duas —
   `worksteal` redistribui itens individuais entre workers ociosos em vez
   de fixar cada arquivo inteiro num worker, o que equilibra melhor uma
   suíte com arquivos de duração muito desigual como `tests/line/`; ainda
   não medido no host Linux). `core/harness/affected.py --run` usa o mesmo
   modo. Testes que não podem paralelizar (estado global, latência
   apertada, porta fixa, mutação do `.factory/` real) levam
   `@pytest.mark.serial` e correm à parte, sequencialmente. Se
   `pytest-xdist` não estiver instalado,
   o runner remove `-n`/`--dist` sozinho com um `[WARN]` e roda serial
   (correto, só mais lento) em vez de falhar.

### Variáveis de ambiente

| Variável | Efeito | Default |
| --- | --- | --- |
| `DARKFAC_HARNESS_STATE_DIR` | Diretório de estado (locks + cache local); deve ser global à máquina, fora de qualquer clone/worktree | `%LOCALAPPDATA%\DarkFac\harness` (Windows) / `${XDG_STATE_HOME:-~/.local/state}/darkfac/harness` (POSIX) |
| `DARKFAC_SUITE_SLOTS` | Nº de execuções completas simultâneas permitidas no host | `1` |
| `DARKFAC_SUITE_LOCK_TIMEOUT_SEC` | Timeout de espera por um slot livre | `3600` |
| `DARKFAC_SUITE_LOCK` | `off` desliga o lock inteiramente | ligado |
| `DARKFAC_SUITE_LOCK_HELD` | Setado automaticamente por quem já detém o lock, para o `pytest` filho não tentar adquirir de novo (não setar manualmente) | — |
| `DARKFAC_SUITE_LOCK_MIN_ITEMS` | Nº mínimo de itens coletados por um `pytest` avulso (não-xdist) para valer o lock; runs pequenos e focados nunca bloqueiam | `100` |
| `DARKFAC_HARNESS_CACHE` | `off` desativa o cache de verdicts (sempre roda fresco) | ligado |
| `DARKFAC_HF02_DATABASE_URL` | Postgres compartilhado (Tailscale) para cache remoto e single-flight cross-host; ausente = local-only | — |
| `DARKFAC_HARNESS_REMOTE_WAIT_SEC` | Teto de espera fazendo polling pelo verdict de outro host | `900` |
| `DARKFAC_HARNESS_INFLIGHT_LEASE_SEC` | Duração do lease da reivindicação `harness_inflight`; renovado por heartbeat a cada `lease/3` enquanto os steps rodam | `90` |
| `CI` | Truthy desativa o cache — CI continua sendo o portão de verdade, sempre fresco | — |
| `PYTEST_XDIST_AUTO_NUM_WORKERS` | Override do nº de workers do `-n auto` (útil na VPS, 2 vCPU) | auto-detectado pelo xdist |

`python core/harness/runner.py --quick --no-cache` força uma execução fresca
pontual sem tocar nas flags de ambiente.

### Notas por host

- **VPS (container Dokploy)**: o diretório de estado fica dentro do
  container por padrão — ou seja, o lock/cache local não sobrevive a um
  redeploy nem é compartilhado entre containers. Para coordenação real entre
  hosts, configure `DARKFAC_HF02_DATABASE_URL` (o cache remoto e o
  single-flight cross-host dependem só do Postgres compartilhado, não do
  disco do container); para persistir o cache local entre redeploys no
  mesmo container, aponte `DARKFAC_HARNESS_STATE_DIR` para um volume
  montado.
- **Windows (Desktop/Notebook)**: o estado vive em
  `%LOCALAPPDATA%\DarkFac\harness`, compartilhado por todos os clones e
  worktrees daquela máquina — é exatamente o ponto: dois worktrees do mesmo
  repositório na mesma máquina disputam o mesmo lock.

## Política de contexto seletivo e promoção de aprendizado (DF-19)

Para prevenir injeção excessiva de tokens e viés de confirmação entre diferentes harnesses:
- **Contexto Bounded (`core.orchestrator.context`)**: O orquestrador sintetiza um resumo operacional delimitado (`TaskContext`), pontuando critérios de aceitação, referências estruturadas de arquivos e marcos de progresso durável. Transcrições brutas de logs nunca são injetadas diretamente no prompt principal.
- **Promoção Fail-Closed (`core.learning.promotion`)**: Candidatos a regras (`LearningCandidate`) permanecem no status `PROPOSED` até serem validados contra uma versão específica da suíte de avaliação (`eval_version`) com pelo menos uma execução empírica comprovada (`supporting_runs`). Regras unpromoted, não avaliadas ou com divergência de versão são estritamente rejeitadas.
- **Rollback Atômico**: Regras ativas que manifestarem regressões são revertidas de forma auditável para `RETIRED`, associadas ao `rollback_ref` e justificativa formal.
