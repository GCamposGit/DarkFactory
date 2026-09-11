---
name: plan-product-architecture
description: Produz PRD, decisões de arquitetura e handoffs pequenos a partir de demanda e baseline verificadas. Use ao planejar projetos, módulos ou correções que exigem contratos claros; separa decisões do owner, desenho técnico e implementação econômica.
---

# Planejamento e especificação para execução

O produto do planejamento é um contrato que outro desenvolvedor consegue implementar e um verificador consegue contestar. Leia missão, regras, roadmap, handoffs vigentes, interfaces e testes relevantes antes de propor substituições. No DarkFac, observar HANDOFF_POLICY.md e HYBRID_AUTONOMY_REQUIREMENTS.md.

## Intenção e Grill

Reutilize decisões conhecidas com origem. Pergunte apenas sobre ambiguidade material que dependa da intenção/conhecimento exclusivo do owner: uma a três perguntas por rodada, alternativas, recomendação e consequência curta, com texto livre. Contexto suficiente satisfaz o Grill sem pergunta artificial.

Diferencie decisão material de preferência opcional. Uma escolha reversível de apresentação ou recorte técnico dedutível pode ser assumida explicitamente, sem bloquear todo o trabalho por conveniência. Silêncio não aprova mudança de produto, gasto ou autorização. Quando uma resposta material for necessária, aguarde somente na parte dependente e preserve o restante preparado.

## Baseline e identidade do plano

- Localize a versão efetivamente disponível no checkout. Um link de outra tarefa ou plano lembrado da conversa não prova que o arquivo está presente nessa branch.
- Registre ticket/parent, origem, versão, documento/hash, baseline relevante e evidência dos predecessores. Distinguir implementado localmente, integrado remotamente, validado no alvo e entregue.
- Revisão do escopo conserva identidade e histórico: registre predecessor, delta, motivo, aprovador e consumidores afetados. Não reutilize o mesmo ID/versão para um contrato diferente sem declarar migração/supersessão.
- Mantenha divergências de fontes visíveis; um relatório “passou” não substitui a entrada/prova que o gerou. Fonte instável ou ausente não vira completude por omissão.

## Problema e arquitetura

Defina problema, usuário, jornada observável, critérios de sucesso e non-goals deste incremento; não transforme uma decisão de escopo atual em proibição eterna. Reuse componentes conferidos. Domínio headless, driver library/cli/http declarado e I/O na borda.

Especifique inputs/outputs, semântica dos campos, ownership, pré/pós-condições, erro e recuperação. Para gates, evidências, persistência, CLI ou processos, leia [padrões de contratos verificáveis](references/contract-review-patterns.md) antes de congelar o handoff. Schema válido e propriedade comprovada são resultados diferentes.

Mapeie requisitos para oráculos: **requisito → origem confiável → dado comparado → ponto de enforcement → caso aceito → caso rejeitado → evidência**. Inclua explicitamente vazio/ausente, conflito, stale, identidade/ticket/versão incompatível e o caminho público usado pelo consumidor quando relevantes. Não exigir uma matriz extensa para uma edição simples que não envolve essas fronteiras.

Decisões de arquitetura pertencem ao planejador qualificado. Não fixe nomes de modelos ou preços históricos como autoridade; resolva capacidades/conta/host no catálogo vigente. Na política DarkFac: alta inteligência planeja e aprova; implementação e testes são econômicos. high/max apenas quando suportados; não elevar silenciosamente o implementador para resolver lacuna do plano.

## Fatiamento e prontidão

Cada unidade normalmente toca de um a quatro arquivos principais, com artefatos gerados declarados. Quantidade de arquivos não mede sozinha complexidade: separar contratos, autoridade, política, transporte e integração quando não couberem numa mudança verificável. Não condensar vários gates num único ticket apenas para respeitar o limite de paths.

Para cada ticket declarar:

- objetivo, decisões resolvidas, deps e artefatos de entrada, paths permitidos/somente leitura e non-goals;
- interface exata e exemplo mínimo válido/inválido; estágios de produção/consumo de cada evidência;
- aceites numerados e `VALIDATE_CMD` exato, distinguindo teste existente de teste a criar;
- ambiente/identidade/rota, configuração por referência segura, permissões e custos autorizados;
- gatilhos, recursos, conflitos, timeout, retry, retomada, fallback, sucessor e condição exclusivamente humana;
- rollback, verificador independente do candidato e critérios de entrega local/remota/operacional aplicáveis.

Um teste novo deve ser criado antes do primeiro uso e falhar pela condição esperada na baseline; arquivo inexistente ou zero testes não é reprodução. Para CLI/JSONL/subprocesso, incluir invocação real a partir de configuração serializada; roundtrip de um objeto vizinho ou chamada in-process não cobre esse trajeto.

Separar `design_specified`, `waiting_dependency`, `needs_architecture_binding` e `ready_for_handoff`. Campos ainda dependentes de runtime/conta precisam de tarefa de alta inteligência que congele APIs, versões, argv e migração. Não marcar ticket executável com escolhas de arquitetura ou placeholders entregues ao econômico. Uma biblioteca pura pode avançar antes de infraestrutura somente com recorte e dependências revistos explicitamente.

## Revisão antes do despacho

Em contratos de confiança, o handoff deve mostrar uma tabela curta com produtor confiável, valor esperado, comparação e API que bloqueia cada garantia. Se só houver flags/referências recebidas do candidato, o desenho ainda está incompleto. Não inventar paths, versões, schemas existentes ou respostas do owner para preencher o template; confirmar no checkout ou marcar a decisão técnica pendente.

Confronte o plano com uma tentativa de resultado incorreto: como um payload, lista vazia, flag, teste parcial ou evento antigo poderia satisfazer o texto sem cumprir a intenção? Corrija o contrato e seus exemplos antes do implementador. Faça revisão independente quando o risco/escopo justificar e estiver autorizada; registre modelo/família observados, sem prometer independência pelo nome de um alias.

Um avaliador que omite controles críticos ou inventa interfaces não qualifica o plano, mesmo que entregue todos os títulos. Registre a falha, corrija o artefato com planejador qualificado e mantenha limitada qualquer afirmação de eficácia da skill; não atribuir aprovação ao teste que falhou.

Atualize índice/hashes/referências após revisão. Integridade detecta alteração; aprovação vem de identidade qualificada no registro confiável. Não criar aprovação humana adicional por ticket quando a autorização existente é suficiente. Relate exatamente o que está especificado, pronto e ainda dependente.
