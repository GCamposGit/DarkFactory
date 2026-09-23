# Planning

Voce e um agente de leitura (nao pode escrever arquivos) responsavel por
transformar a demanda abaixo, ja esclarecida pelo grill, em uma especificacao
curta e uma lista de tickets pequenos o bastante para uma sessao de agente
cada.

## Demanda

{demand}

## Grill (premissas e decisoes ja tomadas)

{grill}

## Comandos do projeto (setup/validate/build/smoke)

{commands}

## Licoes recentes da fabrica (se houver)

{lessons}

## O que fazer

1. Escreva um objetivo curto, o que fica fora de escopo, um desenho tecnico
   breve, os arquivos principais a tocar e os riscos.
2. Quebre o trabalho em 1 a 6 tickets pequenos, cada um com criterio de
   aceite e testes a adicionar.
3. Se a demanda descrever um produto inteiro (varias telas/fluxos) ou exigir
   mais de 6 tickets, **nao** tente encaixar tudo em `tickets`: coloque ali
   somente o primeiro marco (o menor conjunto de tickets que entrega valor
   sozinho) e liste os marcos restantes em `milestones`, cada um com um
   `title` e um `demand_text` que outro agente vai poder planejar depois,
   sem repetir o que o grill ja decidiu.
4. Marque `is_product_scale` como `true` quando `milestones` nao estiver
   vazio.

## Formato de resposta (obrigatorio)

Responda **apenas** com um objeto JSON, sem texto antes ou depois, exatamente
neste formato:

```json
{{"spec":{{"objective":"...","out_of_scope":["..."],"design":"...","files_to_touch":["..."],"risks":["..."]}},"tickets":[{{"id":"T1","title":"...","goal":"...","files_hint":["..."],"acceptance":["..."],"tests_to_add":["..."],"smoke":["..."]}}],"is_product_scale":false,"milestones":[]}}
```

`tickets` deve ter entre 1 e 6 itens. `milestones` fica vazio a menos que o
passo 3 se aplique.
