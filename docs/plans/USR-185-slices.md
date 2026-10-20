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

## Fatia 2 - Migracao dos ids (ENTREGUE em 20/10/2026; 2b abaixo)

Entregue: leitores de codigo passam a usar o registro (`resolve`, novo `resolve_known` para modelos `deprecated`, `id_of`,
`forbidden_markers`): `core/line/routing.py` (marcadores proibidos, pins `development.codex|grok`), `core/line/agent_cli.py`
(`_DEFAULT_OPENROUTER_MODEL` = role `distill.openrouter`), `.factory/config/line_routing.json` (pins/openrouter_model como
referencia de role, resolvidas no carregamento; lista proibida vem do registro), `core/line/subagent_routing.py`
(`subagent_model_routing.json` sem `models`; chave legada ainda aceita), `core/router/model_router.py`,
`core/portfolio/router_optimizer.py`, `core/usage/adapters.py` (modelo Antigravity). Novos aliases no registro:
`grok-review`, `grok-dev`, `opus-legacy`; fallback embutido agora espelha o registro semeado (teste de paridade).
Testes: `tests/test_model_registry_readers_usr185.py` (tabela antes/depois). Censo: 604/107 arquivos -> 554/105.

### Fatia 2b (ENTREGUE em 20/10/2026, parcial no que sobrou)

Entregue: campo `gateway_ids` no registro (ids com prefixo de provedor/gateway; so nomeacao, nao altera roteabilidade) e
`gateway_id()` em `core/models/registry.py`; `resolve`/`resolve_known`/`id_of` tambem aceitam o id de gateway. Novos modelos
sem papel (nao roteaveis): `deepseek/deepseek-v4-pro`, `gemini-3.1-pro`, `gemini-3.1-flash-lite-image`, `gpt-image-2`
(`candidate`) e `claude-3-7-sonnet`, `deepseek/deepseek-r1`, `gemini-2.0-flash-001` (`deprecated`). Migrados:
`core/workflow/qualified_routes.py`, `core/execution/resilience.py`, `core/content/engine.py`, `core/game/cli.py`,
`core/visual/cloud_engine.py`, `core/acceptance/engine.py`, `hub/backend/service.py` (3 ids do catalogo OpenRouter) e
`forbidden_autonomous_models` removido de `subagent_model_routing.json` (ausente = marcadores do registro).
Ids de fio de `core/usage/adapters.py` (User-Agent, `anthropic-beta`, modelo da sonda de quota, ids de quota Antigravity)
ficam literais, marcados com `# census: wire-id` (excluidos do censo; justificativa no comentario).
Censo: 554/105 arquivos -> 530/104.

Sobrou: `hub/data/*.json` (apenas prosa de descricao de servicos, nao lida como id de modelo); ids sem entrada
(`qwen*`, `gpt-oss*`, `gpt-luna-xhigh`, `opus-5.1`, `mistralai/mistral-nemo`, `meta-llama/*`, `nvidia/*`) mantidos literais;
dados de `executors.json`/roadmap/skills/docs (fatia 4); protegidos (fatia 3); aliases de catalogo `gemini-flash`,
`gemini-pro`, `gpt-review` em `qualified_routes.py`.

## Fatia 3 - Proposta unica para arquivos protegidos (PROPOSTA ENTREGUE, aguarda OA-046)

Um documento em `docs/proposals/` com o diff dos protegidos (AGENTS.md, FACTORY_RULES.md, `core/harness/test_subagent.py`)
trocando ids por referencia ao registro; owner publica uma vez. Aceite: proposta unica aplicavel; apos o push, nenhum id
literal restante nos protegidos; teste de invariantes referenciando o registro.

Entregue em 20/10/2026: `docs/proposals/USR-185-F3-protected-registry.{md,patch}` (AGENTS.md, CLAUDE.md, FACTORY_RULES.md, `core/harness/test_subagent.py`, `remote_worker.py`, `suite_lock.py`), `tests/test_model_registry_invariants_usr185.py` (piso 15%, fable/astra, revisor de outra familia) e OA-046 para o owner publicar. Novos `gateway_ids`/aliases no registro dao os mesmos ids de fio. Pendente: publicacao pela OA-046.

## Fatia 4 - Geracao de docs com teste de deriva (ENTREGUE em 20/10/2026, parcial no que sobrou)

Entregue: `core/models/docgen.py` + `scripts/generate_model_docs.py [--check]` (idempotente) reescrevem blocos entre `<!-- BEGIN/END model-registry:<secao> -->` (secoes `roles`, `catalog`). Docs com marcadores: `docs/MODEL_SELECTION_GUIDE.md` (roles, catalog) e `03-model-router/SKILL.md` em `.agents` e `.claude` (roles, espelhos identicos). Teste: `tests/test_model_docs_drift_usr185.py`. O censo ignora blocos gerados. Censo: 530/104 arquivos -> 530/104 (tabelas geradas nao contam).
Sobrou: `executors.json`/roadmap (dados lidos por outros consumidores; sem derivacao segura nesta fatia), demais skills/docs com ids em prosa, protegidos (fatia 3).

Escopo original:

Gerar de `model_registry.json` as secoes de docs/skills que listam modelos (marcadores BEGIN/END). Aceite: comando
idempotente; teste de deriva falha se o doc gerado divergir do registro.

## Fatia 5 - Descoberta (ENTREGUE em 20/10/2026)

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
