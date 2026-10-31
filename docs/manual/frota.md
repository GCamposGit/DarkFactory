<!-- covers: core/line/fleet_dispatcher.py -->
# Operação da frota

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

## Gancho do sentinela da linha (USR-439)

O despachante contínuo integra o gancho do sentinela (`sentinel_hook`) executado em `self.run_sentinel_hook()` a cada ciclo de 30 segundos, delegando a `maybe_run_sentinel()` com janela de silêncio determinística de 45 minutos (2700s). Isso assegura vigilância contínua da saúde da frota e disparo de alertas ao Telegram caso a linha seja paralisada por esgotamento de cotas de IA ou jobs estagnados.
