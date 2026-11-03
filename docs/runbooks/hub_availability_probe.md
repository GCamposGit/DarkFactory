# Sonda de disponibilidade do Hub (DarkFac-Hub-Availability, USR-207/USR-448)

Mede `/health` do Hub a partir de fora da VPS (Notebook, 1x/minuto) e alerta o Telegram de
operacoes quando fica indisponivel por mais de 120s; envia um aviso de recuperacao quando volta.
Codigo: `core/infra/availability_probe.py`. Estado (ultima falha observada, se ja alertou):
`hub_availability.json` em `core.paths.state_root()`.

## Como foi perdida (e como nao perder de novo)

Em 24/10/2026 (USR-377 S2) uma varredura de codigo morto apagou `core/infra/availability_probe.py`
porque nenhum import *dentro do repositorio* apontava para ele -- a Tarefa Agendada do Windows que
a aciona e invisivel a essa busca. A tarefa continuou rodando e retornando codigo 1 a cada minuto
(USR-448). O modulo foi restaurado e agora esta listado em
`.factory/config/external_entrypoints.json`, validado por `core/infra/external_entrypoints.py` e
`tests/test_usr448_external_entrypoints.py`: qualquer varredura futura deve consultar esse registro
antes de remover algo sem chamador visivel no grep.

## Instalar ou corrigir a tarefa (idempotente)

Rode a partir do checkout principal (nunca de uma worktree; `scripts/install_availability_probe.ps1`
recusa com codigo 2 se detectar que a `WorkingDirectory` seria uma worktree):

```powershell
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\dev\DarkFac\scripts\install_availability_probe.ps1
```

O script resolve `pythonw.exe` de forma absoluta (falha fechada se nao existir no PATH nem ao lado
de `python.exe`), nunca usa `Start-Process` nem `Invoke-Expression`, e registra a tarefa com
`Register-ScheduledTask -Force` -- rodar de novo so reafirma a mesma configuracao.

## Verificar

```powershell
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\dev\DarkFac\scripts\install_availability_probe.ps1 -Verify
```

Imprime se a tarefa existe, o caminho e argumentos que ela executa, a `WorkingDirectory` e o
`LastTaskResult`; sai com 0 so quando a tarefa existe e `LastTaskResult = 0`. Equivalente manual:

```powershell
Get-ScheduledTask -TaskName DarkFac-Hub-Availability | Get-ScheduledTaskInfo | Select-Object LastTaskResult
```

## Verificar disponibilidade real sem risco de alerta falso

```powershell
python -m core.infra.availability_probe --check
```

Faz uma unica medicao de `/health`, imprime `OK`/`DOWN` e sai com 0 sem gravar estado nem enviar
alerta -- use para confirmar que a sonda funciona depois de reinstalar a tarefa, sem arriscar
disparar ou mascarar um alerta real.

## Remover

```powershell
Unregister-ScheduledTask -TaskName DarkFac-Hub-Availability -Confirm:$false
```

Depois, remova a entrada `hub-availability-probe` de `.factory/config/external_entrypoints.json`
(e o modulo, se a sonda deixar de ser necessaria) para que a varredura de codigo morto nao fique
com um registro de guarda sem efeito.
