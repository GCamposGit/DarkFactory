# SPEC

## Objetivo
Executar o ticket PH do piloto pela linha completa no repositório privado GCamposGit/pilot-hello, com implementação no checkout/ambiente do produto, revisão independente, PR, CI própria verde, merge squash e limpeza, registrando SHAs, link de CI e host executor, sem usar portão ou runtime do core no produto.

## Fora de escopo
- Execução real do ticket PH nesta rodada (somente leitura)
- Deploy e smoke publicados (M5)
- Prova completa por clone limpo (M6)
- Relatório consolidado (M7)
- Alteração de arquivos, criação de recursos ou execução de comandos nesta rodada
- Uso do portão ou runtime do core dentro do produto

## Desenho
Plano de execução em fases: (1) identificar o ticket PH vinculado ao projeto pilot-hello e à demanda de M3, verificando evidências M1/M2 em docs/evidence/USR-279-M1.md e USR-279-M2.md; (2) preparar checkout próprio de GCamposGit/pilot-hello com Python 3.12+, venv do produto e comandos setup/validate/build/smoke do descritor; (3) implementar o ticket PH no checkout do produto; (4) acionar revisão independente em família diferente do implementador, registrando implementador, revisor e SHA revisado; (5) abrir PR no repositório privado e aguardar CI própria do produto verde para o SHA final do PR, verificando ausência de conflito com main; (6) executar merge squash e limpar branch local/remota e worktree temporária, preservando checkout principal e evidências; (7) registrar PR, SHA do PR, SHA do merge, link do run de CI e host executor. A fábrica orquestra externamente; o produto não recebe runtime, imports, serviços nem portão do core.

## Arquivos a tocar
- docs/evidence/USR-279-M1.md
- docs/evidence/USR-279-M2.md
- docs/evidence/USR-279-M4.md
- scripts/line_validate.py
- requirements.txt

## Riscos
- CI própria do produto não ficar verde para o SHA final do PR
- Conflito com main impedir o merge squash
- Revisor independente não pertencer a família diferente do implementador
- Limpeza de branch/worktree remover evidências ou checkout principal
- Vazamento de runtime/portão do core para dentro do produto
- Identificação incorreta do ticket PH vinculado ao projeto pilot-hello
