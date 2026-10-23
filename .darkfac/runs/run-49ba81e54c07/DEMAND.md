# DEMAND

- projeto: darkfac
- canal: line

## Texto original

# Hub: health do projeto quebra com RoadmapHealth sem atributo status

## Problema
Em 22/10 GET https://darkhub.ggcampos.com/api/portfolio/projects/pilot-hello devolveu health_status=healthy com health_details.error informando que o objeto RoadmapHealth nao tem atributo status: o calculo de saude do portfolio acessa um atributo inexistente e mascara o erro como healthy.

## Jornada
Em 22/10 GET https://darkhub.ggcampos.com/api/portfolio/projects/pilot-hello devolveu health_status=healthy com health_details.error informando que o objeto RoadmapHealth nao tem atributo status: o calculo de saude do portfolio acessa um atributo inexistente e mascara o erro como healthy.

## Fora de escopo
- Nao criar funcionalidade nova alem dos criterios de aceite deste ticket
- Nao redesenhar o painel de portfolio nem o modelo RoadmapHealth alem do atributo que falta

## Criterios de aceite
- GET /api/portfolio/projects/ID nao traz erro de atributo em health_details
- Falha real no calculo de saude nao vira healthy (teste de regressao)
- Portao oficial runner.py --quick passa
