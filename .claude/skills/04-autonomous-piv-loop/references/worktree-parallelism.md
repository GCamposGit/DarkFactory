# Protocolo fail-closed de worktrees paralelas

Estes controles são obrigatórios quando duas ou mais frentes podem escrever no mesmo
repositório e quando uma entrega isolada retorna ao checkout de integração.

### WT-01 — Checkpoint de integração limpo

Eleja um coordenador e um checkout de integração que não execute tarefas. Antes do
fan-out, confirme `git status --porcelain=v1` vazio e grave o SHA-base. Se a raiz ou o
checkout de integração estiver dirty, pare: não despache e não use
`startingState: working-tree`.

### WT-02 — Fingerprint e backup recuperável

Antes de sanear qualquer estado dirty, preserve fora do índice de integração um backup
recuperável contendo `git diff --binary`, status porcelain, arquivos untracked e hashes.
Registre o fingerprint formado por SHA, dirty-state e hashes. Nunca descarte deltas para
obter uma baseline limpa.

### WT-03 — Identidade exclusiva e persistente

Antes da escrita, persista em manifesto o ticket ID definitivo, título definitivo,
owner, branch, caminho absoluto/cwd da worktree, SHA-base e lease. `clientThreadId`
de setup não é o task ID final: resolva e persista o ID real. IDs ou títulos
temporários, campos vazios, branch detached ou reutilização de branch/worktree/owner
bloqueiam o despacho.

### WT-04 — Lease com fencing e heartbeat

Cada frente possui lease exclusivo, expiração e fencing token monotônico. Emita
heartbeat periódico em intervalos de no máximo 60 segundos com task ID, owner, cwd, HEAD, fase,
último comando e horário. Dois intervalos sem revisão nova exigem auditoria do task;
não presuma progresso apenas porque o status é `active`. Lease vencido, heartbeat
ausente ou token antigo bloqueia escrita, conclusão e integração.

### WT-05 — Ownership por arquivo

O manifesto lista cada arquivo permitido e seu owner. Antes da primeira escrita e de
toda expansão de escopo, adquira lock de ownership e compare todos os manifestos
ativos. Sobreposição de arquivo, diretório compartilhado ou lock concorrente bloqueia
o despacho; não resolva a colisão escolhendo silenciosamente um escritor.

### WT-06 — Preflight executável

Registre caminho e versão do Python, confirme `python -m pytest --version`, prove um
`--basetemp` novo, exclusivo e gravável, colete a suíte existente e valide os comandos
focais que já existem. Quando o critério do ticket introduz um teste focal novo,
registre a ausência esperada antes da escrita e prove coleta geral não vazia ou um
teste adjacente existente; não execute o caminho inexistente. Após a primeira escrita,
o novo teste focal deve existir e coletar ao menos um caso antes de ampliar a
implementação. Python sem pytest, coleta geral vazia, PermissionError ou executor
setup/refresh travado são falhas de ambiente: imponha timeout, preserve diagnóstico e
não inicie a implementação.

### WT-07 — Escrita e commit seletivos

Escreva apenas arquivos possuídos. Compare `SHA-base..HEAD`, índice e estado residual.
O commit contém somente o delta da frente; nunca inclui baseline herdada, arquivos de
outro owner ou alterações replicadas por `startingState: working-tree`.

### WT-08 — Contrato de conclusão

A resposta final entrega ticket, task ID, owner e lease; SHA-base e SHA final; branch e
cwd; `git show --name-status`; arquivos; testes focais e obrigatórios com contagem e
exit codes; heartbeat final; relatório; e `git status --short` residual explicado.
Task sem resposta final é incompleta. Texto sem commit verificável não autoriza
integração; commit sem o restante do contrato exige auditoria direta.

### WT-09 — Handoff protegido

É proibido fazer handoff para a raiz/check-out de integração dirty ou com escritor
ativo. Encerre o escritor, preserve backup e restabeleça checkpoint limpo. Não tente
desanexar ou trocar uma branch com alterações locais. Se um handoff parcial falhar,
audite stash, branch, worktree, cwd, lease e reachability antes do retry; reutilize a
identidade persistida e nunca encadeie handoffs às cegas.

### WT-10 — Retry com RCA

Falha ou resultado incerto de criação, executor setup/refresh, teste ou handoff gera
RCA antes do retry. Defina timeout e limite de tentativas; reconcilie o estado existente
em vez de criar outra task/worktree. Uma substituta só pode nascer após provar que a
anterior não escreve mais e registrar a troca no manifesto. Um segundo erro de detach
não autoriza force nem perda de estado.

### WT-11 — Integração topológica e validação conjunta

Com o checkout de integração limpo, integre commits seletivos em ordem de dependência
por cherry-pick ou merge explícito. Após cada commit, rode os testes focais afetados;
ao final, rode conjuntamente os dois comandos obrigatórios, drift de skills, whitespace
e marcadores de conflito. Um gate vermelho interrompe a cadeia.

### WT-12 — Limpeza somente após alcance

Remova worktree, branch temporária, lease e backup somente quando o commit seletivo
estiver alcançável pela branch de integração, os gates focais e conjuntos estiverem
verdes e não houver delta exclusivo no estado residual. Resolva e confira cada caminho
absoluto antes da remoção. Preserve tudo quando faltar prova; remoção forçada não é
mecanismo de limpeza.
