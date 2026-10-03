# Runbook — Worker primario de testes no Desktop (HF-27-11)

Este runbook e para o Owner (voce), rodando UMA VEZ no Desktop
(`darkfac-desktop`, Tailscale `100.78.181.90`) para transformar essa
maquina no worker primario de testes da fabrica: a partir de agora, quando
qualquer harness (Claude Code, Codex, Grok, Antigravity) em qualquer
maquina (VPS, Notebook) rodar `python core/harness/runner.py --quick`, a
suite inteira e despachada para rodar AQUI, liberando as outras maquinas.

Nao assumimos nenhuma experiencia previa com PowerShell, Task Scheduler ou
linha de comando. Siga os passos na ordem. Cada passo diz exatamente o que
clicar/digitar e o que voce deve ver quando der certo.

Leva uns 5-10 minutos. Precisa ser feito so uma vez (o script e seguro de
rodar de novo se algo der errado no meio).

## Indice

1. Abrir uma janela de comando no Desktop (local ou remoto)
2. Rodar o instalador
3. O que "deu certo" parece
4. Conferir a tarefa agendada (Task Scheduler)
5. Testar de outra maquina (opcional, mas recomendado)
6. Solucao de problemas
7. Worker de LINHA no Desktop (V3): passos minimos

---

## 1. Abrir uma janela de comando no Desktop

Se voce ja esta sentado na frente do Desktop fisicamente, va direto para o
passo 1b. Se voce vai acessar remotamente, siga o passo 1a primeiro.

### 1a. Acessando o Desktop remotamente (Google/Chrome Remote Desktop)

1. No seu computador atual, abra o navegador Chrome e va para
   `https://remotedesktop.google.com/access`.
2. Faca login com a conta Google que voce ja usou para configurar o acesso
   remoto ao Desktop (se ainda nao configurou, siga as instrucoes na
   propria pagina antes de continuar este runbook).
3. Na lista de computadores, clique no nome do Desktop (geralmente algo
   como "DESKTOP-XXXXXXX" ou o nome que voce deu na hora de configurar).
4. Digite o PIN de acesso remoto quando solicitado.
5. Aguarde a tela do Desktop aparecer dentro do navegador. A partir daqui,
   voce esta "dentro" do Desktop — continue no passo 1b.

### 1b. Abrindo o PowerShell

1. Clique no botao **Iniciar** do Windows (canto inferior esquerdo, o
   icone com a janela do Windows) — ou aperte a tecla Windows no teclado.
2. Digite `PowerShell` (sem aspas). O Windows vai mostrar "Windows
   PowerShell" na lista de resultados.
3. **Clique com o botao direito** em cima de "Windows PowerShell" e
   escolha **"Executar como administrador"** (Run as administrator).
   - Por que administrador? O instalador cria a regra de firewall (porta
     8080 so para o Tailscale) e registra a tarefa para rodar "mesmo sem
     usuario conectado", para o worker voltar sozinho depois de um reboot
     do Desktop sem ninguem fazer login. As duas coisas exigem
     administrador. Se voce abrir sem administrador, o instalador para no
     inicio com uma mensagem clara e nao altera nada.
4. Uma janela azul (ou preta, dependendo da versao do Windows) vai abrir,
   com um texto tipo `PS C:\WINDOWS\system32>`. Essa e a janela onde voce
   vai colar os comandos dos proximos passos.
5. Se aparecer uma caixa de dialogo perguntando "Deseja permitir que este
   aplicativo faca alteracoes no dispositivo?" (User Account Control),
   clique em **Sim**.

## 2. Rodar o instalador

1. Na janela do PowerShell que voce acabou de abrir, cole o comando
   abaixo (botao direito do mouse cola o conteudo da area de
   transferencia na maioria dos terminais Windows; ou aperte
   `Ctrl+Shift+V`) e aperte **Enter**:

   ```powershell
   cd C:\dev\DarkFac
   ```

   - Se aparecer um erro dizendo que o caminho nao existe, o repositorio
     ainda nao foi clonado nesta maquina — sem problema, o proprio
     instalador (passo seguinte) clona automaticamente. Nesse caso, so
     pule para o proximo comando.

2. Cole e rode:

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\dev\DarkFac\scripts\install_test_worker.ps1
   ```

   - Se voce quiser proteger o worker com uma senha/token (recomendado, mas
     opcional — o firewall ja restringe o acesso so a maquinas da mesma
     rede Tailscale da fabrica), rode esta variante no lugar, trocando
     `SUBSTITUA_POR_UM_TEXTO_LONGO_E_ALEATORIO` por um texto qualquer
     dificil de adivinhar (guarde esse valor, o suporte tecnico vai
     precisar dele para configurar os outros hosts):

     ```powershell
     powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\dev\DarkFac\scripts\install_test_worker.ps1 -Token "SUBSTITUA_POR_UM_TEXTO_LONGO_E_ALEATORIO"
     ```

3. O script vai imprimir varias linhas comecando com `[OK]`, `[INFO]`,
   `[WARN]` ou `[FAIL]`, uma para cada uma das 7 etapas (verificar
   Python/git, baixar/atualizar o codigo, instalar dependencias, registrar
   a tarefa agendada, configurar o firewall, configurar o token opcional,
   e testar se o worker respondeu). Isso leva de 1 a 5 minutos dependendo
   da velocidade da internet (baixar dependencias Python na primeira vez
   pode demorar um pouco mais).

## 3. O que "deu certo" parece

Perto do final, voce deve ver um bloco assim (os valores exatos podem
variar):

```
======================================================================
 SUMMARY
======================================================================
 Repo path        : C:\dev\DarkFac
 Scheduled Task   : DarkFac Test Worker (Task Scheduler Library root, triggers: at logon + at startup)
 Worker command   : C:\...\pythonw.exe core\harness\remote_worker.py --host 0.0.0.0 --port 8080 --node-id darkfac-desktop
 Local health URL : http://127.0.0.1:8080/health
 Tailnet URL      : http://100.78.181.90:8080/health  (from another tailnet host)
 Firewall rule    : created (TCP 8080 from 100.64.0.0/10)
 Auth token       : not set (open on tailnet)
 Result           : OK -- the Desktop is ready as the primary test worker.
======================================================================
```

Se a ultima linha diz **"Result : OK"**, terminou — pode fechar a janela do
PowerShell (e a conexao remota, se estiver acessando de fora).

Se a ultima linha diz **"Result : COMPLETED WITH WARNINGS"**, va para a
secao 6 (Solucao de problemas) e procure pela linha que comeca com `[WARN]`
ou `[FAIL]` no meio da saida — ela diz exatamente o que fazer.

## 4. Conferir a tarefa agendada (Task Scheduler)

Isso e opcional (o passo 3 ja confirma que o worker esta rodando), mas se
voce quiser ver com os proprios olhos que a tarefa foi criada:

1. Aperte a tecla Windows, digite `Agendador de Tarefas` (ou `Task
   Scheduler`, dependendo do idioma do Windows) e clique no resultado.
2. No painel do meio (ou clicando em **Biblioteca do Agendador de
   Tarefas** / "Task Scheduler Library" no painel esquerdo), procure na
   lista por **"DarkFac Test Worker"**.
3. Clique nela uma vez para selecionar. No painel inferior (aba "Geral" /
   "General"), confirme:
   - **Status**: deve dizer "Pronto" / "Ready" ou "Em execucao" /
     "Running".
4. Clique na aba **"Gatilhos" / "Triggers"** (painel inferior) — deve
   listar dois gatilhos: "At log on" e "At startup".
5. Para forcar a tarefa a rodar agora (por exemplo, depois de resolver um
   problema), clique com o botao direito em "DarkFac Test Worker" na lista
   e escolha **"Executar" / "Run"**.

## 5. Testar de outra maquina (opcional, mas recomendado)

Se voce tiver acesso a outra maquina que ja esta na mesma rede Tailscale da
fabrica (o Notebook, por exemplo), abra um terminal la e rode:

```powershell
Invoke-RestMethod -Uri "http://100.78.181.90:8080/health"
```

(ou, se for Linux/macOS: `curl http://100.78.181.90:8080/health`)

Se aparecer um bloco de texto com `status`, `node_id: darkfac-desktop`,
`platform_family: windows`, etc., o worker esta acessivel pela rede
Tailscale e pronto para receber jobs de qualquer host.

## 6. Solucao de problemas

**"[FAIL] Python 3.12 was not found"**
O worker precisa do Python 3.12, a mesma versao do CI. Se a maquina tem
outro Python (por exemplo 3.11, usado pelo open-webui), ele continua
instalado e intocado: o 3.12 entra ao lado dele. No PowerShell de
administrador, rode:

```powershell
winget install -e --id Python.Python.3.12
```

Se o `winget` perguntar sobre os termos da loja, digite `Y` e Enter. Depois
**feche a janela**, abra um PowerShell de administrador novo (passo 1b) e
rode o instalador de novo (passo 2). O instalador acha o 3.12 sozinho pelo
lancador `py`.

**"[FAIL] git pull failed ... local changes would be overwritten"**
Alguem (outra sessao ou agente) editou arquivos do repositorio no Desktop
sem commitar. Para ver o que mudou:

```powershell
git -C C:\dev\DarkFac status --short
```

Para guardar essas edicoes de lado sem perder nada (da para recuperar
depois com `git stash list`):

```powershell
git -C C:\dev\DarkFac stash push -u -m desktop-local-changes
```

Depois rode o instalador de novo (passo 2).

**"[FAIL] git was not found on PATH"**
Instale o Git para Windows baixando de
`https://git-scm.com/download/win` (aceite as opcoes padrao do instalador).
Feche e reabra o PowerShell e rode o instalador de novo.

**"[SKIPPED] Not running as Administrator -- the firewall rule was NOT
created"**
Feche a janela do PowerShell atual, repita o passo 1b garantindo que
escolheu **"Executar como administrador"** dessa vez, e rode o comando do
passo 2 de novo (e seguro rodar quantas vezes precisar).

**"[FAIL] http://127.0.0.1:8080/health did not respond within 20s"**
Primeiro abra o log do worker, que registra o erro mesmo rodando sem
janela:

```powershell
Get-Content "$env:LOCALAPPDATA\DarkFac\worker\daemon.log" -Tail 40
```

Se o erro falar de biblioteca faltando (`ModuleNotFoundError`), reinstale
as dependencias no ambiente proprio do worker (o Python global da maquina
nao e usado):

```powershell
cd C:\dev\DarkFac
& "$env:LOCALAPPDATA\DarkFac\worker\venv\Scripts\python.exe" -m pip install -r requirements.txt
```

e leia o erro que aparecer. Depois de corrigir (geralmente e so rodar o
comando de novo, ou checar a conexao com a internet), rode o instalador do
passo 2 de novo.

Para ver o erro direto na tela, rode o worker manualmente:

```powershell
cd C:\dev\DarkFac
& "$env:LOCALAPPDATA\DarkFac\worker\venv\Scripts\python.exe" core\harness\remote_worker.py --host 127.0.0.1 --port 8080 --node-id darkfac-desktop
```

**Avisos do pip sobre open-webui, pdfplumber ou Pillow**
Versoes antigas deste instalador colocavam as dependencias da DarkFac no
Python global e podiam rebaixar o Pillow usado por outros programas. O
instalador atual usa um ambiente proprio em
`%LOCALAPPDATA%\DarkFac\worker\venv` e nao mexe mais no Python global.

Isso abre o worker no proprio terminal (sem esconder a janela) e mostra
qualquer erro diretamente. Aperte `Ctrl+C` para parar depois de ver o
problema, resolva (geralmente falta alguma biblioteca — a mensagem de erro
diz qual), e volte a rodar o instalador do passo 2.

**Preciso trocar o token (`-Token`) depois de ja ter instalado**
Rode o comando do passo 2 de novo, so que com a variante que inclui
`-Token "novo-valor"`. O instalador substitui a tarefa agendada e a
variavel de ambiente sem duplicar nada.

**Processo legado na porta 8080 (recuperacao autonoma e manual — USR-71)**
Se um processo antigo estiver segurando a porta 8080 sem expor `git_sha` ou
`restart_safe` no `/health`:
- **Autonomo**: o `core.infra.node_sync` detecta o estado legado (`is_legacy`)
  e dispara a recuperacao pela Scheduled Task (`schtasks /End` e `schtasks /Run`),
  aguardando a convergencia sem intervencao humana.
- **Manual** (caso deseje reiniciar manualmente):
  ```powershell
  schtasks /End /TN "DarkFac Test Worker"
  schtasks /Run /TN "DarkFac Test Worker"
  ```
  E confira se voltou com a versao e SHA:
  ```powershell
  Invoke-RestMethod http://127.0.0.1:8080/health | Select-Object status, node_id, git_sha, restart_safe, harness_version
  ```

**Preciso desinstalar / parar o worker**
Abra o PowerShell como administrador e rode:

```powershell
Stop-ScheduledTask -TaskName "DarkFac Test Worker"
Unregister-ScheduledTask -TaskName "DarkFac Test Worker" -Confirm:$false
Remove-NetFirewallRule -DisplayName "DarkFac Test Worker (TCP 8080, Tailscale)"
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\DarkFac\worker\venv"
```

## 7. Worker de LINHA no Desktop (V3): passos minimos

Alem de rodar testes (secoes 1-6), o Desktop pode processar jobs da linha de
producao (grill, planning, development, review, integration, build_deploy) como
um segundo worker, lendo a fila do Postgres da VPS pela tailnet. Sao dois
programas diferentes: o worker de testes (porta 8080, tarefa "DarkFac Test
Worker") continua como esta; o worker de linha e o `cloud_worker`.

Pre-requisitos (uma vez):

1. **Postgres na tailnet**: o compose `darkfac-cloud` precisa ter sido
   reimplantado com o servico `darkfac-pg-tailnet` (ver
   `docs/runbooks/HF-27-09_topology.md`, secao 2). Teste no PowerShell do Desktop:

   ```powershell
   Test-NetConnection 100.83.176.60 -Port 5432
   ```

   Deve mostrar `TcpTestSucceeded : True`. Se der `False`, o servico ainda nao
   subiu (ou o Tailscale do Desktop esta desconectado): pare aqui.
2. **Dependencias no venv do worker de testes** (o mesmo `venv` do passo 2 deste
   runbook, que ja tem `requirements.txt`). Sem `psycopg` o worker cairia em um
   banco em memoria e nunca veria a fila:

   ```powershell
   & "$env:LOCALAPPDATA\DarkFac\worker\venv\Scripts\python.exe" -m pip install "dbos==2.31.1" "psycopg[binary]>=3.2.0,<3.3.0" "psutil>=6.0.0"
   ```

3. **CLIs logadas** (cada comando deve responder sem pedir login):

   | CLI | Como conferir | Se falhar |
   |---|---|---|
   | `claude` | `claude -p "ok"` | rode `claude` e faca login |
   | `codex` | `codex login status` | rode `codex login` |
   | `gh` | `gh auth status` | rode `gh auth login` (GitHub.com, HTTPS) |
   | `git` | `git --version` | instale o Git for Windows |

   `harness:grok` e `harness:antigravity` entram nas capacidades automaticamente
   se `grok`/`antigravity` estiverem no PATH; nao sao obrigatorios.
4. **Variaveis de ambiente do usuario** (defina uma vez; Iniciar > "Editar as
   variaveis de ambiente para sua conta" > Novo...; feche e reabra o
   PowerShell depois):

   | Variavel | Valor | Para que |
   |---|---|---|
   | `DARKFAC_HF02_DATABASE_URL` | `postgresql://USUARIO:SENHA@100.83.176.60:5432/BANCO` (mesmo usuario/senha/banco do worker da VPS; so o host muda para o IP da tailnet) | fila da linha |
   | `GITHUB_TOKEN` | saida de `gh auth token` | clone e push HTTPS (a linha so usa esta variavel) |
   | `DOKPLOY_API_URL`, `DOKPLOY_API_KEY` | os mesmos do worker da VPS | o estagio `build_deploy` pode ser reivindicado pelo Desktop; sem isto o deploy falha |
   | `TELEGRAM_OWNER_BOT_TOKEN`, `TELEGRAM_AUTHORIZED_USERS`, `TELEGRAM_AUTHORIZED_CHATS` | os mesmos do worker da VPS | perguntas do Grill e alertas no Telegram |
   | `DARKHUB_PUBLIC_URL` | `https://darkhub.ggcampos.com` | links das mensagens do Grill |

   O repositorio em `C:\dev\DarkFac` deve estar em `main` atualizado (o worker
   executa o codigo desse checkout): no PowerShell, rode
   `cd C:\dev\DarkFac` e depois `git pull --ff-only origin main`.

Comando unico para subir o worker de linha (janela aberta, `Ctrl+C` para parar):

```powershell
$env:PATH = "$env:LOCALAPPDATA\DarkFac\worker\venv\Scripts;$env:PATH"; powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\dev\DarkFac\scripts\start_onprem_worker.ps1 -Priority secondary -MaxSlots 2
```

O que voce deve ver: um quadro `[DarkFac On-Premises Worker Launcher (HF-27-09)]`
com `Capabilities` contendo `git,gh,node,python,harness:claude,harness:codex,...`
e depois os logs do `cloud_worker`. Uma linha `harness:claude` ausente significa
que o `claude` nao esta no PATH ou nao esta logado.

Como conferir que o worker esta lendo o banco certo (outro PowerShell):

```powershell
& "$env:LOCALAPPDATA\DarkFac\worker\venv\Scripts\python.exe" -c "import sys; sys.path.insert(0, r'C:\dev\DarkFac'); from core.line.owner_intake import open_line_store; s = open_line_store(); print(type(s).__name__, 'mock=', getattr(s, 'mock_mode', None))"
```

Deve imprimir `PostgresControlStore mock= False`. Se der erro `StoreUnavailableError`,
a URL ou a rede estao erradas.

Para deixar de pe apos reboot: `scripts\install_onprem_worker_user_startup.ps1`
(sem administrador) ou `scripts\install_onprem_worker_service.ps1` (administrador);
para parar: `scripts\stop_onprem_worker.ps1`. Os dois instaladores chamam o worker
com o `python` do PATH global: se usar o venv acima, instale `dbos`, `psycopg` e
`psutil` tambem no Python global antes.

Prioridade `secondary`: o Desktop so reivindica jobs que o worker primario da
VPS nao pegou nos primeiros ~30s, entao a VPS continua sendo a primeira
escolha e o Desktop absorve o excesso ou assume se a VPS cair.

## Referencia tecnica

Para quem quiser entender o que o instalador faz por baixo (nao precisa
ler isso para simplesmente instalar): `scripts/install_test_worker.ps1`
(codigo-fonte, com comentarios), e `docs/HARNESS_INTEROP.md`, secao
"Worker primario de testes (Desktop)" (protocolo, variaveis de ambiente,
regras de fallback).
