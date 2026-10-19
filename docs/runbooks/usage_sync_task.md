# Tarefa agendada DarkFac-Usage-Cloud-Sync (USR-236)

A tarefa sincroniza o consumo de cotas com o DarkHub (`scripts/sync_usage_to_cloud.py --loop`). Ela deve apontar sempre para `C:\dev\DarkFac`, nunca para uma worktree (incidente de 03-10/10/2026).

## Instalar ou corrigir (idempotente)

Rode a partir do checkout principal:

```powershell
python C:\dev\DarkFac\scripts\install_usage_sync_task.py --check
python C:\dev\DarkFac\scripts\install_usage_sync_task.py --apply
```

- `--check` so informa a acao (create, keep, update, refuse); exit 0 = nada a fazer, 1 = mudanca necessaria, 2 = recusado.
- `--apply` registra ou atualiza com `schtasks /Create /F /XML`. Se o caminho atual da tarefa difere, ela e re-registrada.
- Rodando de uma worktree o instalador recusa (exit 2).

## Verificar

```powershell
schtasks /Query /TN DarkFac-Usage-Cloud-Sync /V /FO LIST
```

Confira que "Tarefa a Executar" usa `pythonw.exe` com `C:\dev\DarkFac\scripts\sync_usage_to_cloud.py` e que "Iniciar em" e `C:\dev\DarkFac`.

## Elevacao

A tarefa e per-user (logon, nivel limitado); normalmente nao exige administrador. Se `schtasks /Create` negar acesso, abra o PowerShell como administrador e repita o `--apply`; nesse caso a sessao principal registra a acao do owner em `.factory/owner_actions/owner_actions.json`.
