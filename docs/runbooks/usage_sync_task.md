# Runbook - Tarefa agendada do medidor de cotas (DarkFac-Usage-Cloud-Sync)

A tarefa `DarkFac-Usage-Cloud-Sync` roda `scripts\sync_usage_to_cloud.py --loop`
no Notebook (host medidor de cotas) e publica as cotas no DarkHub a cada 60 s.
Ela precisa apontar SEMPRE para o checkout principal `C:\dev\DarkFac`, nunca para
uma worktree. Em 10/10/2026 ela ficou 7 dias apontando para
`C:\Users\guigc\.codex\worktrees\usr-124-postmerge`, rodando codigo velho em
`SYNC_DEGRADED` sem ninguem perceber (ticket USR-236, fatia 5 do USR-206).

O instalador `scripts\install_usage_sync_task.py` e seguro por padrao: sem
`--apply` ele so le a tarefa atual (somente leitura) e imprime o plano.

## 1. Abrir o PowerShell (no Notebook)

1. Clique no botao **Iniciar** do Windows (ou aperte a tecla Windows).
2. Digite `PowerShell` (sem aspas) e aperte **Enter**.
3. Uma janela azul abre. Nao precisa ser administrador na primeira tentativa
   (veja a secao 6 sobre elevacao).

## 2. Ver o plano (somente leitura, nao altera nada)

```powershell
python C:\dev\DarkFac\scripts\install_usage_sync_task.py
```

Leia a linha `Acao:`:

| Acao | Significado | O que fazer |
|---|---|---|
| `keep` | A tarefa ja esta correta | Nada |
| `create` | A tarefa nao existe | Passo 3 |
| `update` | Existe, mas esta errada (ex.: `aponta para worktree: ...`) | Passo 3 |

Os motivos aparecem logo abaixo, um por linha (`aponta para worktree`,
`script diferente`, `argumentos diferentes`, `diretorio de trabalho diferente`,
`executavel diferente`, `configuracao diferente`, `definicao ilegivel`).

## 3. Registrar ou corrigir a tarefa

```powershell
python C:\dev\DarkFac\scripts\install_usage_sync_task.py --apply
```

Saida esperada no final: `Resultado: tarefa registrada e verificada`. O script
registra com `schtasks /Create /XML ... /F`, depois le a tarefa de volta e confere
que ela ficou igual ao desejado. Se a leitura divergir, sai com codigo 1.

Rodar de novo com a tarefa correta e seguro: sai com `Acao: keep` e nao altera nada.

## 4. Trocar o processo em execucao (so quando a acao foi `update`)

Registrar de novo NAO interrompe um loop que ja esteja rodando com o caminho antigo
(a tarefa usa `IgnoreNew`). Pare a instancia antiga e inicie a nova:

```powershell
schtasks /End /TN "DarkFac-Usage-Cloud-Sync"
schtasks /Run /TN "DarkFac-Usage-Cloud-Sync"
```

## 5. Verificar (comandos exatos)

1. O plano tem de dar `keep`:

   ```powershell
   python C:\dev\DarkFac\scripts\install_usage_sync_task.py
   ```

2. A definicao real no Windows (confira `Command`, `Arguments` e `WorkingDirectory`;
   nenhum caminho pode conter `.worktrees`, `.claude\worktrees` ou `.codex\worktrees`):

   ```powershell
   schtasks /Query /TN "DarkFac-Usage-Cloud-Sync" /XML
   ```

3. O processo em execucao tambem tem de apontar para `C:\dev\DarkFac`:

   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Select-Object ProcessId, CommandLine
   ```

4. O log local tem de mostrar `[SYNC_SUCCESS]` recente (e nao `[SYNC_DEGRADED]`):

   ```powershell
   Get-Content C:\dev\DarkFac\.factory\logs\usage_sync.log -Tail 20
   ```

## 6. Elevacao (administrador)

- Caminho normal: NAO exige administrador. A tarefa e do proprio usuario (logon
  `InteractiveToken`, nivel `LeastPrivilege`, gatilho `LogonTrigger` do proprio
  usuario), o mesmo desenho que `scripts\install_sync_task.ps1` ja registra sem
  checar administrador.
- Pode exigir administrador quando o Windows responder `Access is denied` /
  `Acesso negado` no `schtasks /Create`: por exemplo, se a tarefa atual foi criada
  por um PowerShell elevado ou se a politica local do Notebook restringe gatilhos de
  logon. Nesse caso o script sai com codigo 1 e a mensagem mostra a causa.
- Para repetir como administrador: **Iniciar**, digite `PowerShell`, clique com o
  **botao direito** em "Windows PowerShell", escolha **Executar como administrador**,
  confirme o aviso do UAC e rode o MESMO comando do passo 3. Use a mesma conta de
  usuario que opera o Notebook: a tarefa e registrada para o usuario que roda o script.

## 7. Opcoes e codigos de saida

| Opcao | Padrao | Efeito |
|---|---|---|
| `--apply` | desligado | Sem ela, so imprime o plano. Somente Windows. |
| `--repo-root <pasta>` | checkout principal do repositorio | Recusado se estiver dentro de `.worktrees`, `.claude\worktrees` ou `.codex\worktrees`, ou se for worktree vinculada. |
| `--python <pythonw.exe>` | `pythonw.exe` ao lado do Python em uso | Executavel sem janela usado pela tarefa. |
| `--target-url <url>` | `https://darkhub.ggcampos.com` | Destino da sincronizacao. |
| `--interval <s>` | `60` | Intervalo do loop em segundos. |
| `--json` | desligado | Relatorio em JSON. |

Codigos de saida: `0` = ok (inclui `keep` e plano sem `--apply`), `1` = falha ao
registrar ou verificar, `2` = argumento ou raiz invalida (ex.: raiz dentro de worktree).

## 8. Observacoes

- Nunca rode `scripts\install_sync_task.ps1` de dentro de uma worktree: ele deriva a
  raiz do proprio caminho e reproduziria o defeito. Use o instalador em Python acima.
- A chave `DARKFAC_TELEMETRY_KEY` nunca entra na tarefa: o cliente le do ambiente do
  usuario ou de `C:\dev\DarkFac\.env` em tempo de execucao.
- A logica de decisao (criar, manter, atualizar) fica em `core\usage\sync_task.py` e
  e testada por `tests\test_usage_sync_task.py`.
