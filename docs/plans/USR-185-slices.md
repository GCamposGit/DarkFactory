# USR-185 - Fatias do registro de modelos autonomo

Meta: trocar ou adicionar modelo sem trabalho manual (99% autonomo). Invariantes (piso de cota 15%, revisor de outra
familia, modelos proibidos como fable) ficam nos arquivos protegidos e sao validadas por teste contra o registro.

## Fatia 1 - Registro, leitor, validador e censo (ENTREGUE)

- `.factory/config/model_registry.json`, `core/models/registry.py` (`resolve`, `list_candidates`, cache, fallback embutido).
- Validador rejeita: modelo proibido em papel autonomo, revisor da mesma familia do implementador, rota para modelo
  nao `active`, piso de cota abaixo de 15%.
- Leitor migrado como prova: `core/line/subagent_routing.py` (`default_config` resolve `subagent.<alias>`).
- `scripts/model_id_census.py`: contagem informativa de ids literais fora da allowlist.
  Contagem na entrega da fatia 1: 600 ocorrencias em 107 arquivos (nao falha ainda).
- Pendencia conhecida: `.factory/config/subagent_model_routing.json` ainda carrega `models` literais; `parse_config`
  continua lendo-os (a fatia 2 os remove e passa a usar apenas o registro).

## Fatia 2 - Migracao dos ids

Migrar leitores restantes (`core/line/routing.py`, `core/router/model_router.py`, `core/portfolio/router_optimizer.py`,
`core/usage`, `core/content`, `core/harness` nao protegido, `line_routing.json`, hub, skills) para `resolve()`.
Aceite: censo cai para o piso da allowlist; testes existentes verdes; comportamento identico.

## Fatia 3 - Proposta unica para arquivos protegidos

Um documento em `docs/proposals/` com o diff dos protegidos (AGENTS.md, FACTORY_RULES.md, `core/harness/test_subagent.py`)
trocando ids por referencia ao registro; owner publica uma vez. Aceite: proposta unica aplicavel; apos o push, nenhum id
literal restante nos protegidos; teste de invariantes referenciando o registro.

## Fatia 4 - Geracao de docs com teste de deriva

Gerar de `model_registry.json` as secoes de docs/skills que listam modelos (marcadores BEGIN/END). Aceite: comando
idempotente; teste de deriva falha se o doc gerado divergir do registro.

## Fatia 5 - Descoberta

Job (sem chamadas pagas por padrao) que lista modelos novos dos provedores e registra como `candidate`, com preco,
contexto e datas. Aceite: fixture offline; nunca promove sozinho; respeita o teto de gasto configurado.

## Fatia 6 - Qualificacao

Bateria deterministica (contrato, ferramentas, tarefas de referencia da fabrica) com orcamento maximo e evidencia
persistida; resultado grava o registro de qualificacao do candidato. Aceite: candidato sem qualificacao aprovada nao
vira `active` (validador); custo respeita o teto.

## Fatia 7 - Promocao e rollback

Promocao `candidate -> active` somente com qualificacao aprovada; rollback automatico para o modelo anterior em regressao
(falha de portao, custo, cota); `deprecated -> retired` por data. Aceite: transicoes auditadas no ledger, rollback testado,
nenhuma edicao manual de arquivos protegidos.

## Fatia 8 - Limiares do Grill

Definir quando o Grill (WAITING_HUMAN) e obrigatorio: modelo proibido/frontier, preco acima do teto, mudanca de familia
em papel de revisao, qualificacao ambigua. Aceite: limiares configurados no registro e cobertos por teste; todo o resto
segue autonomo.
