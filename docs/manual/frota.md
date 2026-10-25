# Operação da frota

O `core.line.fleet_dispatcher` consulta a fila continuamente e despacha tickets `planned`
para grokbox, Notebook e Desktop. O serviço não depende de sessão interativa. Uma reserva
de host tem prazo; quando o host desaparece, o próximo reconciliador expira a reserva e
o job volta a ser elegível para admissão em outro host. A indisponibilidade de uma VM é
tratada como falha normal de infraestrutura, sem concluir o ticket.

## Notebook e Desktop (Windows)

Instale a tarefa a partir do checkout principal atualizado, nunca de uma worktree. Abra
PowerShell como o usuário que executará o serviço e rode:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\dev\DarkFac\scripts\install_fleet_dispatcher.ps1
```

O instalador pede a senha da conta atual para registrar uma tarefa do Agendador com
gatilho `At startup` e logon `Password`. O Agendador guarda a credencial protegida pelo
Windows; a tarefa roda com privilégios mínimos, espera a rede quando disponível, e
reinicia após falha a cada minuto. A conta precisa ter leitura do checkout e acesso às
variáveis/credenciais operacionais necessárias. O log fica em
`C:\dev\DarkFac\.factory\runs\fleet_dispatcher_logs\fleet-dispatcher.log`, com cinco
arquivos rotacionados de até 10 MiB cada.

Verifique em `Agendador de Tarefas > Biblioteca do Agendador de Tarefas >
DarkFac-Fleet-Dispatcher > Propriedades > Disparadores` que o disparador é “Ao iniciar o
sistema”. Em `Geral`, confira a conta efetiva e “Executar estando o usuário conectado ou
não”. Para ver atividade, abra a aba `Histórico` e consulte o log rotativo acima.

## grokbox (systemd)

O arquivo versionado é [`scripts/fleet-dispatcher.service`](../../scripts/fleet-dispatcher.service).
O exemplo assume usuário/grupo `darkfac`, checkout em `/opt/darkfac` e ambiente virtual
em `/opt/darkfac/.venv`. Ajuste esses caminhos para o checkout implantado antes de
instalar. Na VM, crie a conta de serviço se ainda não existir, garanta que o checkout e
ambiente virtual pertençam a ela, instale a unit e crie o arquivo de ambiente:

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
sudo systemctl status fleet-dispatcher.service
```

Na unit, confirme `User`, `Group`, `WorkingDirectory`, `ExecStart` e
`DARKFAC_FLEET_LOG_DIR` contra os caminhos reais. No arquivo de ambiente, adicione cada
variável operacional necessária ao checkout e aos workers. Campos opcionais para
endereços customizados são `DARKFAC_FLEET_URL_GROKBOX` e
`DARKFAC_FLEET_URL_DESKTOP` (valores são URLs base HTTP dos workers; defaults já estão
no código). Não copie tokens ou senhas para a unit nem para este repositório; injete-os
no arquivo local com permissões `0640` e propriedade `root:darkfac`.

`Restart=always` mantém o serviço ativo após encerramento ou falha do processo; systemd
inicia-o em cada boot. Os logs da aplicação são gravados em
`/var/log/darkfac/fleet-dispatcher.log`, com cinco cópias rotacionadas de até 10 MiB.
Para observar o serviço: `sudo journalctl -u fleet-dispatcher.service -f`.

Se grokbox ou a VM ficar indisponível, não apague nem altere manualmente a reserva:
heartbeats cessam, o TTL expira, o reconciliador libera o slot e o job volta a ser
elegível. Confira `fleet_reservations`/estado da fila e os logs do despachante após o
host retornar. `Restart=always` cobre falhas do processo; recuperação de host morto é
feita pelo lease e pela admissão normal da frota.
