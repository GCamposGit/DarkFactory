# DEMAND

- projeto: darkfac
- canal: line

## Texto original

# Grill: preservar decisoes materiais pendentes sem defaults por prazo ou limite de perguntas

## Problema
Inspecao estatica em b585a191addce4be54a4abd0f051d8e587949e02: core/line/stage_grill.py:760-765 envia perguntas do owner excedentes a MAX_QUESTIONS para technical; linhas 964-980 resolvem por prazo e finalizam mesmo sem decisao explicita. core/demands/grill.py:338-343,549 tambem resolve perguntas sem resposta automaticamente. Contraria AGENTS.md secao 2 para ambiguidade material. Nao foi reproduzido incidente em producao. Pesquisa/evidencia: .factory/research/20261101_continuous_quality_loops/INSIGHTS.md, secao 7. Corrigir comportamento em codigo sem alterar governanca protegida.

Owner autorizou encaminhamento da implantacao e reaproveitamento deste defeito. Bloqueio anterior: rebase_conflict: docs/FASE_ATUAL.md. Worktree preservada esta limpa e sem ownership ativo; retomar pelo caminho existente e resolver eventuais conflitos tecnicos autonomamente. Aceite auto pela politica vigente.

## Jornada
Inspecao estatica em b585a191addce4be54a4abd0f051d8e587949e02: core/line/stage_grill.py:760-765 envia perguntas do owner excedentes a MAX_QUESTIONS para technical; linhas 964-980 resolvem por prazo e finalizam mesmo sem decisao explicita. core/demands/grill.py:338-343,549 tambem resolve perguntas sem resposta automaticamente. Contraria AGENTS.md secao 2 para ambiguidade material. Nao foi reproduzido incidente em producao. Pesquisa/evidencia: .factory/research/20261101_continuous_quality_loops/INSIGHTS.md, secao 7. Corrigir comportamento em codigo sem alterar governanca protegida.

## Fora de escopo
- Nao alterar governanca protegida ou conceder permissoes novas.
- Nao resolver decisoes materiais por silencio; preservar automacao tecnica e canarios explicitamente autorizados.

## Criterios de aceite
- Demanda real com objetivo, sucesso, negocio ou aceite material pendente permanece waiting_human apos prazo; silencio nao registra escolha do owner.
- Mais de tres decisoes materiais sao preservadas para rodadas seguintes; limite de exibicao nao as converte em tecnicas.
- Perguntas tecnicas reversiveis e politica explicita de canario permanecem automaticas, com origem de decisao registrada.
- Consolidar testes comportamentais existentes para timeout, perguntas excedentes e canario; atualizar manual; passar portao oficial e revisao cruzada na entrega.
