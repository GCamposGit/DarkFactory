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

**"[FAIL] python was not found on PATH"**
Instale o Python 3.12 (ou mais novo) baixando de
`https://www.python.org/downloads/windows/`. Durante a instalacao, marque
a caixa **"Add python.exe to PATH"** na primeira tela do instalador (muito
importante — se pular essa caixa, o Windows nao vai achar o Python depois).
Depois de instalar, feche e reabra o PowerShell (passo 1b) e rode o
instalador de novo (passo 2).

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
Normalmente significa que alguma dependencia Python nao instalou direito.
Na mesma janela do PowerShell, rode:

```powershell
cd C:\dev\DarkFac
python -m pip install -r requirements.txt
```

e leia o erro que aparecer. Depois de corrigir (geralmente e so rodar o
comando de novo, ou checar a conexao com a internet), rode o instalador do
passo 2 de novo.

Se a instalacao das dependencias funcionou mas o worker ainda nao
responde, rode manualmente para ver o erro na tela:

```powershell
cd C:\dev\DarkFac
python core\harness\remote_worker.py --host 127.0.0.1 --port 8080 --node-id darkfac-desktop
```

Isso abre o worker no proprio terminal (sem esconder a janela) e mostra
qualquer erro diretamente. Aperte `Ctrl+C` para parar depois de ver o
problema, resolva (geralmente falta alguma biblioteca — a mensagem de erro
diz qual), e volte a rodar o instalador do passo 2.

**Preciso trocar o token (`-Token`) depois de ja ter instalado**
Rode o comando do passo 2 de novo, so que com a variante que inclui
`-Token "novo-valor"`. O instalador substitui a tarefa agendada e a
variavel de ambiente sem duplicar nada.

**Preciso desinstalar / parar o worker**
Abra o PowerShell como administrador e rode:

```powershell
Stop-ScheduledTask -TaskName "DarkFac Test Worker"
Unregister-ScheduledTask -TaskName "DarkFac Test Worker" -Confirm:$false
Remove-NetFirewallRule -DisplayName "DarkFac Test Worker (TCP 8080, Tailscale)"
```

## Referencia tecnica

Para quem quiser entender o que o instalador faz por baixo (nao precisa
ler isso para simplesmente instalar): `scripts/install_test_worker.ps1`
(codigo-fonte, com comentarios), e `docs/HARNESS_INTEROP.md`, secao
"Worker primario de testes (Desktop)" (protocolo, variaveis de ambiente,
regras de fallback).
