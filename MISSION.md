# DarkFac Mission

## Objetivo
Uma fábrica de software autônoma: recebe uma demanda em linguagem natural, esclarece com o operador apenas o que não consegue resolver sozinha (Grill) e entrega o produto ponta a ponta para o cliente final (código, testes, revisão independente, PR, CI, merge, deploy e smoke), sem intervenção humana fora da demanda inicial e do Grill. O mesmo núcleo evolui a própria fábrica (dogfood).

## Princípios
- Headless, determinístico e portátil (Windows e Linux); um clone limpo roda a validação e descobre as skills sem credenciais privadas.
- Nenhum sucesso sem efeito verificável (SHA, PR, deploy, smoke); o CI do GitHub é a verdade de `main`.
- Entradas humanas só para intenção, negócio, segredos/contas e aceite comercial.
- Pesquisa online em melhores fontes e repositórios para melhores práticas e state of the art das features implementadas sempre que necessário.

## Escopo versionado
`core/`, `hub/`, `.agents/skills/` e espelho `.claude/skills/`, `tests/`, `docs/`, scripts de bootstrap e CI.

## Fora do escopo compartilhado
Chaves, tokens, dados pessoais, configurações específicas de máquina, pesos de modelos e artefatos regeneráveis.

## Critério de sucesso
Uma demanda enviada pelo Telegram ou DarkHub vira PR com testes, merge, deploy e smoke verde com relatório no Telegram; o canário diário passa; o `main` permanece verde em Linux e Windows.