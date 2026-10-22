# DEMAND

- projeto: darkfac
- canal: line

## Texto original

# Publicacao de protegidos por aprovacao no Hub (fim do ritual manual de portao+push)

## Problema
Em 21-22/10 cinco OAs (041/045/046/050/052) exigiram do owner o mesmo ritual: worktree, git apply de patch, portao local de 30-90 min, push direto ou merge --admin. Houve 3 tentativas falhas (runner apontando para o checkout sujo, timeout local por concorrencia, worker travado). O USR-228 ja criou o registro de autorizacao do owner (.factory/governance/authorizations/<id>.json na base confiavel) que o guard aceita. Falta o fluxo: agente abre PR com o patch protegido; o Hub mostra botao 'Aprovar publicacao protegida' que grava o registro vinculado a ticket, caminhos exatos e sha do candidato; a esteira roda o portao remoto e mergeia sozinha.

## Jornada
Em 21-22/10 cinco OAs (041/045/046/050/052) exigiram do owner o mesmo ritual: worktree, git apply de patch, portao local de 30-90 min, push direto ou merge --admin. Houve 3 tentativas falhas (runner apontando para o checkout sujo, timeout local por concorrencia, worker travado). O USR-228 ja criou o registro de autorizacao do owner (.factory/governance/authorizations/<id>.json na base confiavel) que o guard aceita. Falta o fluxo: agente abre PR com o patch protegido; o Hub mostra botao 'Aprovar publicacao protegida' que grava o registro vinculado a ticket, caminhos exatos e sha do candidato; a esteira roda o portao remoto e mergeia sozinha.

## Fora de escopo
- Nao relaxar o guard nem permitir autoaprovacao por agente

## Criterios de aceite
- OA de protegido vira um unico clique de aprovacao no Hub com diff visivel
- Registro de autorizacao amarrado a ticket, caminhos e sha; sha diferente invalida
- Portao roda em worker remoto e merge acontece sem comando do owner
- Teste prova que flag de ambiente sozinha continua nao autorizando
- Portao oficial runner.py --quick passa
