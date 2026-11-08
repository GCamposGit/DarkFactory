<!-- covers: core/line/fleet_dispatcher.py, core/line/pending_delivery.py -->
<!-- covers: core/line/fleet_dispatcher.py, core/line/recovery_receipt.py -->
# Operação da frota

Na elegibilidade local, um worker com memória suficiente é aceito (USR-452).
Zero workers permanece indisponível. O gate ajusta seu prazo ao paralelismo real,
sem forçar processos além da capacidade e sem executar suítes concorrentes.
## Espera de rota e retomada automática (USR-473)

Sem assinatura elegível, o roteador tenta OpenRouter com saldo confirmado, dentro do
orçamento vigente e preservando capacidade e revisão por outra família. Sem saldo ou
rota, o launcher grava a espera em `.factory/pending_delivery/<ticket>.json` antes de
sair com código75. O dispatcher permanece ativo: espera não depende de um chat ou
heartbeat de LLM. O serviço verifica snapshots e crédito, sem gerar texto para supervisionar.

A espera registra etapa, worktree, implementador e próximo horário. Cada espera é
consultada no máximo a cada15 minutos; o estado sobrevive ao reinício. REVIEW usa
`run_ticket.py --resume-delivery` na mesma worktree; o launcher preserva validação
vinculada ao candidato. PLAN/BUILD usam a retomada canônica do ticket, com suas reservas.
Processo vivo não é duplicado, ticket terminal sai da fila e falha diferente de
`route_wait` fica bloqueada com causa explícita. Decisão material `waiting_human`
continua dependendo do owner. Nenhuma espera compra créditos nem aumenta o teto.

Durante recuperação, o mesmo serviço admite só retomadas:

```powershell
Set-Location C:\dev\DarkFac
python -m core.line.fleet_dispatcher --loop --resume-only
```

O modo `--resume-only` não admite novos BUILDs e não executa sentinela/digest adicionais.
A variável persistente `DARKFAC_FLEET_RESUME_ONLY=1` também ativa esse modo no boot.
A tarefa Windows `DarkFac-Fleet-Dispatcher` mantém boot e reinício em falha; não substitua
esse serviço por inferência recorrente. Ao sair da recuperação, remover `--resume-only`
da ação da tarefa e limpar `DARKFAC_FLEET_RESUME_ONLY` libera novamente a fila regular.

Para uma recuperação que não termina em ociosidade, confira primeiro a interface aceita
pela CLI e passe a lista como um único valor separado por vírgulas:

```powershell
python -m core.line.fleet_dispatcher --help
python -m core.line.fleet_dispatcher --loop --recovery-tickets "USR-1,USR-2"
```

`--recovery-tickets` mantém a ordem e dá prioridade aos itens da lista. Com
`recovery.active=false`, a lista não serializa nem bloqueia a frota: depois dela o dispatcher
continua pela fila regular e usa os hosts livres, sempre respeitando reservas e `suite_lock`.
Com `recovery.active=true`, o modo global de recuperação mantém no máximo uma entrega em
andamento e a lista segue serializada. Retomadas em execução têm precedência.

O estado `recovery_list` em `last_cycle` informa `pending` e `cause`. A lista encerra com
`recovery_list_completed` quando os itens ficam terminais ou `recovery_list_expired` ao
vencer o TTL. Se nenhum item estiver admissível, publica `recovery_list_no_admissible`, mas
não bloqueia a fila regular. Ticket ausente ou pendência material não contam como concluídos.
Não passe os IDs como argumentos posicionais separados: a CLI recusa a chamada com código 2
antes de iniciar o ciclo.

Quando uma entrega falha depois de preservar um candidato confiável, o launcher retoma a
entrega nessa worktree (`_resume_delivery`), sem repetir PLAN ou BUILD. Se não houver sinal
confiável de desenvolvimento concluído, mantém o fluxo normal do ticket.

`completed` no ledger não encerra uma entrega cujo recibo ainda diz `deployed=false`.
O controlador mantém `merged_awaiting_deploy`, executa o deploy canônico e só encerra
a espera após verificar o SHA saudável de produção incluindo o candidato. Falha de
deploy permanece pendente para nova tentativa, sem chamar um agente para supervisionar.

O `core.line.fleet_dispatcher` consulta a fila continuamente e despacha tickets `planned`
para grokbox, Notebook e Desktop. O serviço não depende de sessão interativa. Uma reserva
de host tem prazo; quando o host desaparece, o próximo reconciliador expira a reserva e
o job volta a ser elegível para admissão em outro host. A indisponibilidade de uma VM é
tratada como falha normal de infraestrutura, sem concluir o ticket.

Cada despacho inicia `run_ticket.py` em um worktree operacional limpo criado de `origin/main`.
O despachante copia para esse checkout somente o registro do ticket selecionado; arquivos
alterados no checkout compartilhado, inclusive estado de runtime, ficam preservados e fora
da execução. Falhas ao atualizar a referência ou criar o worktree aparecem como falha de
lançamento no estado do ciclo e colocam o ticket em cooldown; não são tratadas como despacho
bem-sucedido.

Ao encerrar um job, ou desfazer um checkout criado parcialmente, o despachante usa
`core.git.ticket_workspace.cleanup`. Antes de remover o registro Git ou os arquivos, a rotina
verifica processos com o diretório atual dentro do checkout. Se encontrar um holder, deixa o
checkout registrado e intacto e grava no log seu caminho e o processo identificado; depois que
ele encerrar, uma execução posterior da limpeza pode removê-lo. Para checkout sem holder, a
limpeza ocorre normalmente após o término do job. A deduplicação do deploy continua agrupando
merges pela janela de silêncio configurada.

## Notebook e Desktop (Windows)

Instale a tarefa a partir do checkout principal atualizado, nunca de uma worktree.
Use uma conta dedicada de serviço nos dois computadores, com senha gerenciada pela
organização e direito de **Log on as a batch job**. Conceda a ela leitura/execução no
checkout e no ambiente virtual, gravação em `.factory\runs` (estado) e na pasta de logs.
Não a torne administradora local. O Python, o checkout e o estado da tarefa devem estar
em caminhos locais permanentes, acessíveis antes de qualquer usuário entrar.

O instalador usa o `python.exe` encontrado no `PATH`, o checkout onde o script está e o
usuário atual como conta de serviço. São os campos `Command`, `WorkingDirectory` e
`UserId` do XML produzido por `core.line.fleet_task_xml.generate_task_xml`. O XML ainda
define gatilho `BootTrigger`, logon `Password`, nível `LeastPrivilege`, uma única
instância e limite de execução ilimitado. Para ajustar os três caminhos/identidade,
altere os valores no instalador e no gerador antes de registrar novamente.

No PowerShell, como a conta de serviço, rode:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\dev\DarkFac\scripts\install_fleet_dispatcher.ps1
```

Informe a senha da conta de serviço quando solicitada. O Agendador armazena a credencial
protegida pelo Windows e executa a tarefa mesmo sem sessão interativa. A tarefa reinicia
após falha a cada minuto, até 999 vezes. O instalador recusa execução a partir de uma
worktree. Para trocar Python, checkout ou conta, ajuste os valores correspondentes e
rode novamente o instalador no checkout principal.

Configure variáveis operacionais no ambiente de sistema ou no ambiente da conta de
serviço antes de iniciar a tarefa. `DARKFAC_FLEET_LOG_DIR` escolhe a pasta absoluta de
log, que precisa permitir gravação para a conta. Sem ela, usa
`C:\dev\DarkFac\.factory\runs\fleet_dispatcher_logs`. O arquivo
`fleet-dispatcher.log` é UTF-8, limitado a 10 MiB com cinco cópias rotacionadas; as
mensagens também seguem para o console do processo. Não coloque segredos em arquivos
versionados ou argumentos da tarefa.

Verifique em `Agendador de Tarefas > Biblioteca do Agendador de Tarefas >
DarkFac-Fleet-Dispatcher > Propriedades`:

- `Geral`: conta de serviço correta, “Executar estando o usuário conectado ou não” e
  “Executar com privilégios mais altos” desmarcado.
- `Disparadores`: “Ao iniciar o sistema”.
- `Ações`: Python completo, argumentos `-m core.line.fleet_dispatcher --loop` e
  diretório de início igual ao checkout.
- `Configurações`: reinício após falha a cada minuto e instâncias simultâneas ignoradas.

Confira `Histórico` e `Último Resultado da Execução`. Para acompanhar tarefa e log:

```powershell
Get-ScheduledTask -TaskName DarkFac-Fleet-Dispatcher | Get-ScheduledTaskInfo
Get-Content C:\dev\DarkFac\.factory\runs\fleet_dispatcher_logs\fleet-dispatcher.log -Tail 100 -Wait
```

Se `DARKFAC_FLEET_LOG_DIR` apontar a outra pasta, use-a no segundo comando.

## Revisão OpenRouter e evidência de contexto (USR-474)

O payload da revisão inclui a SPEC e os critérios de aceite do ticket, conteúdo real
relevante da worktree dentro dos limites configurados e sanitizado, além de um manifesto
com caminhos, tamanhos e hashes para verificar o material enviado. O OpenRouter não lê
arquivos da worktree por ferramenta; a revisão só pode aprovar o contexto efetivamente
transportado. Contexto ausente recusa a revisão como `review_context_unavailable`, e uma
resposta truncada recusa como `review_output_truncated`.
Somente uma resposta completa, com `truncated=false`, pode sustentar aprovação; saída
truncada não é evidência de revisão concluída.

O evento de uso atribui `ticket_id` a partir da requisição ou de `DARKFAC_TICKET_ID`
para relacionar tokens e custo ao ticket. Se essa identidade não puder ser determinada, a
atribuição fica ausente e deve ser tratada como lacuna de evidência, sem inferência a
partir do prompt ou da resposta. A resposta normalizada preserva `finish_reason` e
`completion_tokens`; `truncated` identifica `finish_reason` `length` ou uma conclusão
de 4096 tokens.

Para recuperação, `--recovery-tickets` aceita exatamente um argumento CSV, preservando a
ordem e ignorando espaços e itens vazios. Confira sem iniciar a frota com
`python -m core.line.fleet_dispatcher --help`; a forma válida é
`python -m core.line.fleet_dispatcher --loop --recovery-tickets "USR-1,USR-2"`.
Passar `USR-1 USR-2` como tokens posicionais separados é erro de CLI (código 2) e é
recusado antes de inicializar o dispatcher ou executar um ciclo.

## grokbox (systemd)

O arquivo versionado é [`scripts/fleet-dispatcher.service`](../../scripts/fleet-dispatcher.service).
Ele aguarda `network-online.target`, usa `User=darkfac` e `Group=darkfac`, trabalha em
`/opt/darkfac` e executa `/opt/darkfac/.venv/bin/python -m core.line.fleet_dispatcher
--loop`. O `Restart=always` reinicia após encerramento normal ou falha, com espera
explícita de dez segundos (`RestartSec=10`). `TimeoutStopSec=30` permite encerramento
limpo, `LogsDirectory=darkfac` cria `/var/log/darkfac` com modo `0750`, e
`WantedBy=multi-user.target` habilita a inicialização no boot.

Os caminhos e a identidade são exemplos ajustáveis: revise `User`, `Group`,
`WorkingDirectory`, `ExecStart`, `EnvironmentFile`, `LogsDirectory` e
`LogsDirectoryMode` para a implantação. Use uma conta de sistema sem shell/login
interativo (`/usr/sbin/nologin`) e conceda acesso de leitura/execução ao checkout e à
venv, gravação ao estado sob `.factory/runs` e à pasta criada para logs. O arquivo de
ambiente fica fora do checkout, pertence a `root:darkfac` e usa modo `0640`.

Na VM grokbox, prepare a conta, checkout e venv; instale a unit e crie o ambiente
externo:

```sh
id -u darkfac >/dev/null 2>&1 || sudo useradd --system --home-dir /opt/darkfac --shell /usr/sbin/nologin darkfac
sudo install -d -o darkfac -g darkfac /opt/darkfac /etc/darkfac
# Atualize/deploy o checkout e crie /opt/darkfac/.venv antes de habilitar o serviço.
sudo install -o root -g root -m 0644 /opt/darkfac/scripts/fleet-dispatcher.service /etc/systemd/system/fleet-dispatcher.service
sudo touch /etc/darkfac/fleet-dispatcher.env
sudo chown root:darkfac /etc/darkfac/fleet-dispatcher.env
sudo chmod 0640 /etc/darkfac/fleet-dispatcher.env
sudoedit /etc/systemd/system/fleet-dispatcher.service
sudoedit /etc/darkfac/fleet-dispatcher.env
sudo systemctl daemon-reload
sudo systemctl enable --now fleet-dispatcher.service
```

No ambiente externo, defina `DARKFAC_FLEET_LOG_DIR=/var/log/darkfac` e as variáveis
operacionais necessárias aos workers. `EnvironmentFile=-/etc/darkfac/fleet-dispatcher.env`
carrega esse arquivo; o prefixo `-` permite iniciar mesmo quando ele ainda não existe.
Os endereços opcionais são `DARKFAC_FLEET_URL_GROKBOX` e
`DARKFAC_FLEET_URL_DESKTOP` (URLs base HTTP; o código tem defaults). Não copie tokens
ou senhas para a unit nem para o repositório; injete segredos apenas no arquivo local,
com permissões restritas. `DARKFAC_FLEET_LOG_DIR` pode apontar a outra pasta absoluta,
desde que o usuário do serviço possa gravar nela e que ela exista.

Os logs são gravados em `$DARKFAC_FLEET_LOG_DIR/fleet-dispatcher.log`, codificados em
UTF-8 e limitados a 10 MiB por arquivo com cinco cópias. A saída também é encaminhada ao
journal. Verifique e consulte os logs:

```sh
sudo systemctl status fleet-dispatcher.service
sudo systemctl is-enabled fleet-dispatcher.service
sudo journalctl -u fleet-dispatcher.service -n 100 --no-pager
sudo journalctl -u fleet-dispatcher.service -f
sudo tail -n 100 -f /var/log/darkfac/fleet-dispatcher.log
```

Se grokbox ou a VM ficar indisponível, não apague nem altere manualmente a reserva:
heartbeats cessam, o TTL expira, o reconciliador libera o slot e o ticket `planned` volta
a ser elegível em outro host. `Restart=always` cobre falhas do processo local; a
recuperação de host morto vem do lease e da admissão normal da frota.

## Frota e VPS enxuta (USR-369)

Regras da VPS (Hetzner CX23, 2 vCPU, 4 GB):

- Todas as imagens sao construidas no CI e publicadas no GHCR. Nenhum `build:` nos compose de `deploy/dokploy/`.
- Soma dos limites de memoria <= 3,2 GB (teste: `tests/shape/test_vps_lean_usr369.py`).
- Concorrencia de build = 1 (`MAX_CONCURRENT_BUILDS` em `scripts/dokploy_redeploy.py`; os disparos sao sequenciais).
- n8n removido da pilha implantada.

## Orcamento de memoria (limites)

| Servico | Compose | Limite |
|---|---|---|
| darkfac-coordinator | cloud | 512M |
| darkfac-worker | cloud | 1536M |
| darkfac-backup-cron | cloud | 256M |
| darkfac-canary | cloud | 192M |
| darkfac-pg-tailnet | cloud | 64M |
| darkhub | hub | 512M |
| Total | | 3072M (teto 3200M) |

## Como restaurar o n8n

Os arquivos foram removidos do repositorio (historico preservado). Para restaurar:

```powershell
git -C C:\dev\DarkFac show a0e0f8d3:deploy/dokploy/docker-compose.n8n.yml
git -C C:\dev\DarkFac show a0e0f8d3:deploy/dokploy/env.n8n.example
```

Recoloque os dois arquivos em `deploy/dokploy/`, recrie o compose no Dokploy e reajuste os limites: o n8n pede 1024M + 256M, o que estoura o teto de 3,2 GB; reduza outros servicos antes e ajuste o teste. Enquanto existir um compose `n8n` ou `darkfac-n8n` no Dokploy, `dokploy_redeploy.py` sem `--only` o ignora; `--only darkfac-n8n` ainda o alcanca.

## Imagem do cloud no GHCR

O job `cloud` de `.github/workflows/hub-image.yml` publica `ghcr.io/gcamposgit/darkfac-cloud:<sha>` (coordinator, worker, backup-cron, canary) junto com a imagem do Hub. O `DARKFAC_GIT_SHA` do compose `darkfac-cloud` e gravado pela propria fabrica: `scripts/dokploy_redeploy.py` (`pin_cloud_git_sha`) acrescenta `DARKFAC_GIT_SHA=<sha de origin/main>` ao env do compose no Dokploy antes de cada deploy, preservando as outras variaveis. Antes disso o script espera a tag `ghcr.io/gcamposgit/darkfac-cloud:<sha>` existir no GHCR (mesmo mecanismo do Hub); se ela nao aparecer no prazo, nao grava o sha novo nem dispara o deploy, e o sha anterior continua rodando. Sem sha valido, o disparo falha fechado em vez de construir na VPS.

Mudancas em `.factory/config/**` (precos, roteamento, registro de modelos) reconstroem a imagem do Hub (USR-397); o ledger (`.factory/demands`, `.factory/owner_actions`) continua somente dados.

## Falhas registradas

Quando um `run_ticket` da frota termina com código diferente de zero, ou o checkout operacional não pode ser preparado, o despachante grava no log (`fleet-dispatcher.log`) e em `recent_failures` do estado (`.factory/runs`) o ticket, o host, o `exit_code`, a classe (`failure`, `infra`, `lock` ou `launch`) e o motivo (últimas linhas do log do ticket ou erro de lançamento, sem segredos; nunca vazio). O histórico guarda as 20 últimas. Erros de lançamento mantêm o ticket em cooldown e não aparecem como despacho concluído.

## Circuit breaker e recuperação (USR-468)

USR-484: o fingerprint de falha usa a causa classificada do log (`[ERRO NA ENTREGA] causa`
ou `cause` JSON, a última vale), nunca o texto variável. No limiar, `gate_failed`,
`gate_dirty_worktree` e `fix_agent_no_changes` seguram só o ticket (hold durável no
admission store; `ticket_hold:<causa>` em `last_cycle`, com resumo em
`python -m core.ops.briefing`), sem ativar recovery global. O diagnóstico lista o hold, a
causa e as ocorrências. Um digest novo de candidato ou contexto libera o hold; esperar não.
`infra`, `no_delivery_evidence` e `recovery_receipt` seguem no disjuntor global.

O recibo é produzido pelo dispatcher depois do deploy canônico e da prova HTTP
do SHA exato. Um marcador escrito pelo processo filho não libera a recuperação.
A entrega pendente fica em `pending_recovery_releases.json`, ao lado do estado do
dispatcher, e é retomada após reinício antes de admitir novos tickets. Validação,
revisão independente, merge e índice de learning packs vêm dos artefatos reais;
CI e saúde são consultados novamente. Falha no deploy ou na prova mantém a espera.

Cada tentativa é identificada por fingerprint de projeto, ticket, candidato, contexto, classe de erro e versão do recurso. O SQLite durável guarda o backoff, a causa, as reservas e a trava global de recuperação. O mesmo fingerprint permanece aberto mesmo depois do backoff; só uma mudança observável no candidato, contexto, recurso ou janela de cota permite nova tentativa. Causas operacionais aparecem em `idle_reasons`, `skipped` e `recovery.receipt_cause`; ausência de rota, worker zero/ocupado/stale, reserva ativa e receipt incompleto são espera, nunca sucesso.

Na recuperação, há no máximo uma entrega em voo para toda a frota. A concorrência normal só volta após uma linha `[RECOVERY_RECEIPT] {JSON}` no log da entrega. O receipt deve ser real (`simulated: false`), recente (até sete dias), vinculado ao digest do candidato em cada estágio e incluir `[HARNESS_PASS]`, revisão independente aprovada, CI verde, SHA de merge alcançável, deploy desse SHA, smoke HTTP 2xx e learning pack indexado. Evidência ausente, stale, simulada ou de outro candidato deixa a trava ativa e registra a primeira causa recusada. O hash do receipt e o candidato recuperado ficam persistidos, tornando o retorno ao teto normal idempotente após reinício.

Para diagnosticar, consulte o estado do ciclo em `.factory/runs` e o SQLite de admissão em `DARKFAC_STATE_ROOT` (ou a raiz de estado configurada). `recovery.active=true` mantém a frota serializada; `recovery.receipt_cause` aponta o estágio que falta. Corrija a evidência ou altere de modo verificável o candidato/recurso e deixe o despachante retomar com backoff. Não apague reservas nem a trava manualmente.

## Gancho do sentinela da linha (USR-439)

O despachante contínuo integra o gancho do sentinela (`sentinel_hook`) executado em `self.run_sentinel_hook()` a cada ciclo de 30 segundos, delegando a `maybe_run_sentinel()` com janela de silêncio determinística de 45 minutos (2700s). Isso assegura vigilância contínua da saúde da frota e disparo de alertas ao Telegram caso a linha seja paralisada por esgotamento de cotas de IA ou jobs estagnados.

USR-473: espera de deploy exige SHA do recibo de merge e evidencia coincidente
do candidato validado e aprovado. Recibo inconsistente permanece visivel como
falha de verificacao. O smoke registra o SHA do ticket e a versao saudavel que
o contem, sem substituir identidade do candidato.

USR-473: identidade de implementacao ausente e provas de deploy ausentes ou
inconsistentes ficam bloqueadas com causa persistida. Contencao do mutex de
retomada impede novas admissoes naquele ciclo; nenhuma cota e consumida em espera.
USR-468: admissao remota combina `/health` (ocupacao) com `/metrics/hardware`
(RAM, CPUs e instante de coleta). Saude generica nunca vira capacidade; RAM
insuficiente continua zero. Corretores de falhas tambem registram sua familia
para impedir revisao pelo mesmo provedor de qualquer implementador.

USR-468 integra o controlador de retomadas USR-473 ao breaker de admissao.
O candidato composto exige revisao independente e portao integral proprios;
rodadas recusadas do USR-473 permanecem preservadas. Completed sem recibo de
deploy nao remove espera; uma retomada ativa impede readmissao do mesmo ticket
mesmo que o inventario externo de processos esteja vazio.

## Recuperacao integrada USR-477

O corretivo preserva as revisoes recusadas de USR-473/468. Completed sem report
de deploy usa merge, gate e revisao coincidentes para retomar o deploy; prova
ausente ou divergente permanece bloqueada. Falha do deploy fica no historico
com codigo e proxima tentativa, sem supervisor LLM. Runtime do harness nao entra
no candidato. O revisor pago usa o alias sonnet, de familia diferente dos
implementadores; familias tambem reconhecem os ids de gateway do registro. A
mudanca responde aos JSONs truncados e achados contraditorios da rota anterior,
sem aumentar rodadas, cota ou orcamento. Resultado real ainda exige gate e deploy.

USR-477: saida zero sem entrega em recovery registra falha e causa persistida.
Retomada de deploy exige prova da familia independente. Se a validacao for
corrompida apos merge, o recibo preserva SHA/PR reais sem digest autorizado;
a ausencia desse digest bloqueia release ate a recuperacao da evidencia.

## Encerramento sem entrega e recuperacao de CI (USR-479)

Retorno zero sem prova de merge gera causa persistida e cooldown nos modos
normal e recovery. O ticket nao ganha uma nova execucao imediatamente.
A correcao de CI e unica por tentativa: o contador permanece consumido ao
retomar, inclusive depois de gate e revisao verdes. Retomar a entrega segue
gate, revisao independente, novo CI e merge; BUILD nao e repetido. O teste
comportamental cobre esse ciclo inteiro na mesma worktree. Revisoes recusadas
de candidatos anteriores permanecem preservadas; o novo candidato exige suas
proprias provas de portao, revisao e producao.
