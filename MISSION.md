# DarkFac Mission

## Objetivo

Manter um núcleo Python headless, determinístico e portátil para orquestração de modelos, pesquisa, conteúdo, ativos visuais, aprendizado contínuo e validação. O projeto deve funcionar depois de um clone limpo em Windows ou Linux e ser compreensível por diferentes harnesses de código.

## Escopo versionado

- `core/`: domínio, serviços headless, roteamento, aprendizado e harness determinístico.
- `hub/`: DarkHub HTTP e frontend estático.
- `.agents/skills/`: catálogo canônico de skills para Antigravity e agentes compatíveis.
- `.claude/skills/`: espelho para Claude Code e harnesses que adotem esse layout.
- `tests/`: testes do núcleo compartilhado.
- `docs/`, scripts de bootstrap e configurações de CI.

## Fora do escopo compartilhado

- Chaves, tokens, credenciais, dados pessoais e configurações específicas de uma máquina.
- Pesos de modelos, caches, imagens/áudios gerados e outros artefatos grandes ou regeneráveis.
- Deploy de produção, billing ou qualquer ação externa não descrita em uma issue.

## Critério de sucesso

Um clone limpo deve conseguir instalar as dependências, executar a validação sintática e os testes do núcleo, iniciar o DarkHub e descobrir as skills sem depender de credenciais privadas.
