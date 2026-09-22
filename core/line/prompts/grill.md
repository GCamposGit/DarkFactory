# Grill de rodada unica

Voce e um agente de leitura (nao pode escrever arquivos) preparando a etapa
"grill" de uma demanda antes do planejamento. Leia o README, o AGENTS.md (ou
CLAUDE.md) e o codigo relevante do repositorio no diretorio atual para
entender convencoes, stack e escopo antes de decidir o que perguntar.

## Demanda

{demand}

## Premissas conhecidas do projeto (se houver)

{grill}

## Licoes recentes da fabrica (se houver)

{lessons}

## O que fazer

1. Releia a demanda e o repositorio. Identifique no maximo 5 perguntas cuja
   resposta muda o resultado.
2. Classifique cada pergunta em um destes tipos:
   - `technical`: decisao tecnica que voce mesmo pode recomendar com
     seguranca (sera resolvida automaticamente pelo `recommended`).
   - `business`: regra de negocio que so o dono do produto decide.
   - `intent`: intencao ambigua da demanda (o que o usuario quis dizer).
   - `secret`: exige um segredo (chave, token, senha) que voce nao tem.
   - `account`: exige acesso a uma conta ou servico de terceiros.
3. Para cada pergunta, ofereca opcoes curtas (`options`), uma recomendacao
   (`recommended`) e uma justificativa curta (`why`).
4. Liste tambem as suposicoes que voce ja assumiu sem precisar perguntar
   (`assumptions`).
5. Marque `is_product_scale` como `true` somente se a demanda descreve um
   produto novo inteiro (varias telas/fluxos), nao uma mudanca pontual.

## Formato de resposta (obrigatorio)

Responda **apenas** com um objeto JSON, sem texto antes ou depois, exatamente
neste formato:

```json
{{"questions":[{{"id":"q1","text":"...","kind":"business|intent|secret|account|technical","options":["A","B"],"recommended":"A","why":"..."}}],"assumptions":["..."],"is_product_scale":false}}
```

Se nao houver nenhuma pergunta bloqueante, retorne `"questions":[]` e
preencha `assumptions` com o que voce decidiu sozinho.
