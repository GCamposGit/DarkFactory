# DEMAND

- projeto: darkfac
- canal: line

## Texto original

# line_validate ignora --help e dispara gate oficial com snapshot

## Problema
Em20/10/2026 a consulta python scripts/line_validate.py --help executou validacao real, criou snapshot a999132e882d e despachou suite oficial job6ade17ebb1a1430ea32ec25d35eabe07. O script nao interpreta argumentos antes de executar main, confundindo consulta de ajuda com acao custosa.

## Jornada
line_validate ignora --help e dispara gate oficial com snapshot

## Fora de escopo
- Nao alterar semantica nem reduzir cobertura do portao oficial.

## Criterios de aceite
- --help imprime uso e sai0 sem criar snapshot nem chamar runner.
- Argumento desconhecido sai2 sem efeitos; chamada sem argumentos conserva validacao oficial.
