# Runbook: Distributed Fleet Workers (USR-286 / USR-277)

## 1. Visão Geral e Topologia da Frota

A frota da Dark Factory opera com capacidade para até **3 ciclos simultâneos**, distribuídos em 3 máquinas físicas/virtuais:

| Host ID | Máquina | Sistema Operacional | Prioridade | Capacidade (MaxSlots) | Papel Típico |
|---|---|---|---|---|---|
| `grokbox` | Grokbox VM | Linux (Ubuntu) | 1 (Primary) | 1 | Runner de alta velocidade (testes em ~2-4 min) |
| `notebook` | AI_Notebook | Windows 11 | 2 (Secondary) | 1 | Máquina de desenvolvimento e orquestração local |
| `desktop` | DESKTOP-G45IPEM | Windows 10/11 | 3 (Fallback) | 1 | Test worker Windows, Docker, GPU local |

### Princípios Operacionais Inegociáveis
1. **1 ciclo por host**: Cada máquina executa estritamente uma demanda completa por vez (preparação, desenvolvimento, validação, revisão, integração e release).
2. **Prioridade não bloqueante**: Se Grokbox estiver livre, recebe a próxima demanda; se estiver ocupado ou em drain, o Notebook é admitido sem espera; se ambos ocupados, o Desktop é admitido.
3. **Desacoplamento de Harness**: Máquina e Harness são decisões distintas. Não fixar Grok no Grokbox ou Codex no Notebook. A escolha de harness por estágio segue `core.line.routing.pick('development')` com piso de cota de 15.0%.
4. **Isolamento de Worktrees**: Cada demanda roda em sua própria worktree isolada (`.worktrees/usr-XXX`).
5. **Autonomia Git, Deploy e Backup (USR-57)**: Nunca solicitar comandos Git manuais (`commit`, `push`, `merge`), deploys ou rotinas de backup ao Owner.

---

## 2. Inventário e Verificação (Probe)

Para inspecionar a prontidão de um host, ferramentas de compilação, harnesses e conexão ao banco Postgres:

```bash
python scripts/start_fleet_worker.py probe --host grokbox --json
```

No PowerShell (Windows):
```powershell
python scripts/start_fleet_worker.py probe --host notebook
.\scripts\start_onprem_worker.ps1 -Probe -HostId desktop
```

O probe verifica:
- Ferramentas no `PATH`: `git`, `gh`, `node`, `python`.
- Harnesses disponíveis: `claude`, `codex`, `grok`, `antigravity`.
- Conectividade com o banco Postgres de controle via Tailscale (`DARKFAC_HF02_DATABASE_URL`).
- Acessibilidade dos caminhos dos projetos registrados em `.factory/projects.json`.

---

## 3. Inicialização e Bootstrap Idempotente

A inicialização de um worker é **idempotente**: chamadas repetidas detectam que o daemon já está ativo e não criam processos duplicados nem resetam o estado em execução.

### Modo Foreground (Console)
```powershell
python scripts/start_fleet_worker.py start --host notebook
```

### Modo Headless (Daemon em Segundo Plano)
```powershell
python scripts/start_fleet_worker.py start --host notebook --headless
```
Ou via wrapper PowerShell:
```powershell
.\scripts\start_onprem_worker.ps1 -HostId desktop -Headless
```

Arquivos de controle gerados:
- PID: `.factory/pids/fleet_worker_<host_id>.pid`
- Log: `.factory/test_logs/fleet_worker_<host_id>.log`

---

## 4. Consulta de Status

Para verificar se o worker está rodando, seu PID e se está em processo de drenagem:

```powershell
python scripts/start_fleet_worker.py status --host notebook
```

Exemplo de saída:
```text
Host ID   : notebook
Running   : True (PID: 14208)
Draining  : False
Priority  : 2 (MaxSlots: 1)
Log File  : C:\dev\DarkFac\.factory\test_logs\fleet_worker_notebook.log
```

---

## 5. Drenagem Graciosa (Drain) e Atualização Segura (Rolling Update)

Para atualizar o código ou reiniciar uma máquina sem interromper os ciclos ativos nas outras:

### Passo 1: Sinalizar Drain no Host Alvo
```powershell
python scripts/start_fleet_worker.py drain --host grokbox --reason "rolling_update"
```
O sinal de drain cria o arquivo `.factory/pids/fleet_worker_<host_id>.drain`. O worker **rejeita novas admissões**, mas permite que o ciclo atualmente em execução termine seu trabalho.

### Passo 2: Monitorar a Conclusão do Ciclo Ativo
```powershell
python scripts/start_fleet_worker.py status --host grokbox
```
Enquanto o ciclo termina, os outros hosts (`notebook` e `desktop`) continuam executando suas respectivas demandas sem qualquer interferência.

### Passo 3: Atualizar o Repositório no Host Alvo
Após o término do ciclo ativo:
```bash
git pull --ff-only origin main
```

### Passo 4: Reiniciar o Worker
```bash
python scripts/start_fleet_worker.py stop --host grokbox
python scripts/start_fleet_worker.py start --host grokbox --headless
```
O arquivo `.drain` é automaticamente removido no restart e o host volta a aceitar novas demandas.

---

## 6. Procedimento de Rollback

Se uma atualização falhar ou a infraestrutura precisar ser congelada:

```powershell
python scripts/start_fleet_worker.py rollback --host notebook
```

O rollback:
1. Suspende a admissão imediata de novas demandas (ativa drain).
2. Preserva todas as worktrees ativas em `.worktrees/`.
3. Preserva todos os logs de evidência em `.factory/test_logs/`.
4. Não apaga branches Git locais nem registros do ledger de demandas.

---

## 7. Tratamento de Acesso Exclusivo do Owner (Owner Actions)

Se durante o inventário ou operação for detectada uma falha de autenticação exclusiva que exija ação humana (ex.: expiração de token do GitHub, reautenticação de harness via navegador ou ajuste de chave de API):

1. **É proibido** pedir ao owner que execute comandos manuais de Git, deploy ou rotinas de backup.
2. O agente deve registrar imediatamente uma **Owner Action estruturada** no arquivo `.factory/owner_actions/owner_actions.json`.
3. A OA deve conter:
   - Identificador único (`OA-XXX`).
   - Título claro e objetivo.
   - Passo a passo detalhado, tela por tela, na interface do provedor.
   - Critério de verificação executável (`verification_command`).
   - Tickets que ficam bloqueados aguardando a ação.
