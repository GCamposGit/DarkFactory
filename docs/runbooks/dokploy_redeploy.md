# Runbook - Deploy pos-merge no Dokploy

Este runbook e para o Owner: como gerar o token de API do Dokploy uma unica
vez e guarda-lo como variavel de ambiente, para que qualquer harness
(Claude Code, Codex, Grok, Antigravity) consiga rodar
`python scripts/dokploy_redeploy.py` depois de qualquer merge em `main` e
redeployar tudo que esta no projeto `darkfac-core`
(`https://dokploy.ggcampos.com`).

Nao assumimos nenhuma experiencia previa com Dokploy ou PowerShell. Siga os
passos na ordem. Leva uns 3-5 minutos e so precisa ser feito uma vez por
maquina (Notebook, Desktop, VPS, etc. -- cada uma que vai rodar o comando
precisa da sua propria configuracao, a menos que voce copie as mesmas
variaveis para as outras).

## Indice

1. Gerar o token de API no Dokploy
2. Guardar o token como variavel de ambiente (Windows, permanente)
3. Rodar o comando
4. O que "deu certo" parece
5. Solucao de problemas

---

## 1. Gerar o token de API no Dokploy

1. Abra o navegador e va para `https://dokploy.ggcampos.com`.
2. Faca login com a sua conta de Owner (usuario/senha ou o metodo que voce
   configurou).
3. No canto superior direito da tela, clique no seu **avatar** (icone
   circular com sua foto ou inicial).
4. No menu que abrir, clique em **Settings** (ou "Profile", dependendo da
   versao da interface -- procure o item que leva as configuracoes da sua
   conta pessoal, geralmente logo abaixo do seu nome/email).
5. Na pagina de configuracoes, procure na lista lateral (ou nas abas do
   topo) por **API/CLI** (pode aparecer como "API Keys", "Access Tokens" ou
   similar -- e a secao que fala em gerar tokens para automacao).
6. Clique no botao **Generate Token** (ou "Create Token" / "New Token").
7. Se aparecer uma caixa de dialogo pedindo um nome/descricao para o token,
   preencha com algo identificavel, por exemplo:
   - **Name/Description**: `darkfac-harness-redeploy`
8. Clique em **Generate** / **Create** / **Confirm** (o rotulo exato do
   botao varia com a versao).
9. Copie o token que aparecer na tela -- ele normalmente so e mostrado uma
   vez. Cole em um lugar temporario seguro (um gerenciador de senhas, por
   exemplo) ate colar no comando do proximo passo.
10. Anote tambem a URL base da API: `https://dokploy.ggcampos.com` (sem
    barra no final).

Se a interface do Dokploy tiver mudado de layout na sua versao e nenhum dos
rotulos acima bater exatamente, procure por qualquer secao que mencione
"API", "Token" ou "CLI" dentro das configuracoes da sua conta -- a estrutura
geral (avatar -> configuracoes -> API) tende a ser estavel entre versoes.

## 2. Guardar o token como variavel de ambiente (Windows, permanente)

Estas variaveis precisam existir no escopo **User** do Windows (nao no
escopo de processo, que some quando voce fecha o terminal) para que
qualquer harness aberto depois -- inclusive em uma sessao nova do Claude
Code, Codex, Grok ou Antigravity -- consiga ler sem voce precisar setar de
novo toda vez.

1. Abra o PowerShell (nao precisa ser como administrador para isto).
2. Cole os dois comandos abaixo, um de cada vez, apertando Enter depois de
   cada um. Troque `COLE_O_TOKEN_AQUI` pelo valor que voce copiou no passo
   1.9 (mantenha as aspas duplas):

   ```powershell
   [Environment]::SetEnvironmentVariable("DOKPLOY_API_URL", "https://dokploy.ggcampos.com", "User")
   [Environment]::SetEnvironmentVariable("DOKPLOY_API_KEY", "COLE_O_TOKEN_AQUI", "User")
   ```

3. Nenhum dos dois comandos imprime nada na tela se der certo -- isso e
   esperado.
4. **Feche a janela do PowerShell atual e abra uma nova** (e feche/reabra
   qualquer sessao de harness que ja estava rodando, incluindo esta sessao
   do Claude Code se for ela mesma quem vai rodar o comando depois) --
   variaveis de ambiente so sao lidas por processos abertos *depois* de
   serem setadas. Um processo que ja estava aberto antes deste passo nao
   enxerga a variavel nova (o script tem um fallback automatico para o
   registro do Windows nesse caso especifico -- ver secao 5 -- mas abrir um
   terminal novo e mais simples e sempre funciona).
5. Para conferir que gravou certo, no terminal novo rode:

   ```powershell
   [Environment]::GetEnvironmentVariable("DOKPLOY_API_URL", "User")
   ```

   Deve imprimir `https://dokploy.ggcampos.com`. (Nao rode o mesmo comando
   trocando para `DOKPLOY_API_KEY` em uma tela compartilhada/gravada -- ele
   imprime o token em texto puro.)

### Linux/macOS (VPS ou outro worker)

Se a maquina que vai rodar o comando for Linux (por exemplo a VPS), adicione
as duas linhas abaixo no final do `~/.bashrc` (ou `~/.profile`, dependendo
do shell) e abra um terminal novo depois:

```bash
export DOKPLOY_API_URL="https://dokploy.ggcampos.com"
export DOKPLOY_API_KEY="COLE_O_TOKEN_AQUI"
```

(No Linux nao existe o fallback de registro do Windows -- a variavel de
ambiente precisa mesmo estar setada no processo que roda o script.)

## 3. Rodar o comando

Com as variaveis configuradas (e um terminal novo aberto), na raiz do
repositorio:

```powershell
python scripts/dokploy_redeploy.py
```

Isso redeploya todos os servicos do projeto `darkfac-core` (compose +
application) no ambiente `production` e espera cada um terminar. Outras
formas uteis:

```powershell
python scripts/dokploy_redeploy.py --list
python scripts/dokploy_redeploy.py --dry-run
python scripts/dokploy_redeploy.py --only darkfac-cloud
```

## 4. O que "deu certo" parece

Uma saida assim, terminando sem nenhuma linha `FAILED to trigger deploy` ou
`status=error`/`status=timeout`:

```
Local origin/main HEAD subject: fix(hub): ...
Triggering redeploy for 4 service(s) in darkfac-core/production...
[darkfac-n8n] deploy triggered (compose, id=...)
[darkfac-cloud] deploy triggered (compose, id=...)
[Darkhub] deploy triggered (compose, id=...)
[darkfac-canary] deploy triggered (application, id=...)
[darkfac-n8n] status=done title='...' elapsed=32.1s
[darkfac-cloud] status=done title='...' elapsed=45.7s [matches local origin/main]
[Darkhub] status=done title='...' elapsed=28.4s
[darkfac-canary] status=done title='...' elapsed=51.2s
```

O comando termina com codigo de saida `0` nesse caso (`echo $LASTEXITCODE`
no PowerShell logo depois confirma). Se algum servico aparecer com
`status=error` ou `status=timeout`, o codigo de saida e `1` -- va no
dashboard do Dokploy (projeto `darkfac-core`, servico em questao, aba
"Deployments") ver o log completo do deploy que falhou.

## 5. Solucao de problemas

**Erro citando `DOKPLOY_API_URL` e/ou `DOKPLOY_API_KEY` ausentes (codigo de
saida 2)**
As variaveis nao foram encontradas nem no processo atual nem (no Windows)
no registro do usuario. Repita a secao 2, confirme com o comando de
`GetEnvironmentVariable` do passo 2.5, e certifique-se de ter aberto um
terminal **novo** depois de setar.

**`HTTP 401` no meio da saida**
O token esta errado, expirado ou foi revogado no Dokploy. Volte a secao 1,
gere um token novo (o anterior pode ser revogado depois na mesma tela de
"API/CLI" do Dokploy) e repita a secao 2 com o valor novo.

**Um servico fica em `status=timeout`**
O deploy no Dokploy esta demorando mais que o timeout padrao (900s = 15
minutos por servico). Confira no dashboard do Dokploy se o deploy ainda
esta rodando (as vezes builds grandes demoram mais na primeira vez, por
exemplo baixando imagens novas). Se so precisar esperar mais, rode de novo
so para esse servico com um timeout maior:

```powershell
python scripts/dokploy_redeploy.py --only NOME_DO_SERVICO --timeout 1800
```

Se o deploy realmente travou (nao aparece progresso no dashboard ha varios
minutos), cancele/reinicie pelo proprio dashboard do Dokploy e rode o
comando de novo depois.

**Erro de rede / DNS / conexao recusada**
Confirme que voce consegue abrir `https://dokploy.ggcampos.com` no
navegador a partir da mesma maquina. Se o navegador tambem nao conseguir,
o problema e de rede/VPN/Tailscale (fora do escopo deste script), nao do
token.

**Quero rodar em outra maquina (por exemplo a VPS) alem da que configurei
primeiro**
Repita a secao 2 (variante Linux/macOS, se for o caso) naquela maquina --
cada maquina que for rodar o comando precisa da sua propria copia das duas
variaveis. O token gerado no passo 1 pode ser reaproveitado em todas elas
(nao precisa gerar um token por maquina, a menos que voce prefira por
seguranca -- nesse caso repita a secao 1 e use um nome diferente por
maquina, por exemplo `darkfac-harness-redeploy-vps`).

## Referencia tecnica

Para quem quiser entender o que o comando faz por baixo (nao precisa ler
isso para simplesmente rodar): `scripts/dokploy_redeploy.py` (codigo-fonte,
com comentarios) e `docs/HARNESS_INTEROP.md`, secao "Deploy pos-merge
(Dokploy)" (contrato entre harnesses, variaveis, exit codes, guarda de
escopo do projeto/ambiente).
