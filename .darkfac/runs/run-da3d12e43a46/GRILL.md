# GRILL

## Premissas
- Esta rodada é somente leitura: não executa tickets, não altera arquivos nem registra ações externas.
- A mudança evolui a fábrica existente; não cria um produto novo nem substitui o DAG, o Postgres ou o portão oficial.
- run_ticket, despachante e linha devem compartilhar implementação e contratos de estágio; os canais apenas submetem, acompanham e respondem.
- Correções pequenas ficam dispensadas do grill quando objetivo, sucesso verificável e modo de aceite já estiverem claros; ambiguidades materiais continuam bloqueantes.
- Decisões técnicas são resolvidas pela fábrica e registradas em ADR; modelos de planejamento vêm do nível alto do registro, sem fallback barato.
- Pesquisa, documentação e learning pack terão contratos e pontos de integração neste motor; a implementação especializada desses estágios permanece no USR-376, conforme a divisão do plano.
- A entrega deste bloco inclui seu próprio manual atualizado e learning pack para leigo e dev, mesmo com USR-376 separado.
- Até a API do USR-343 estar disponível, a fila deve usar o arquivo canônico versionado com persistência fora da imagem do container e um único alimentador.
- Retomadas preservam decisões e artefatos válidos; indisponibilidade transitória de rota gera espera técnica, sem novo grill.
- project_id deve acompanhar todo o ciclo, incluindo roteamento, workspace, validação e entrega; contratos específicos de projetos externos permanecem aplicáveis.
- A remoção de planning-only limita-se aos blocos restantes do plano vigente e preserva dependências e demais requisitos de elegibilidade.

## Decisoes do owner
- (nenhuma pergunta bloqueante)

## Aguardando decisao do owner
- [intent] O critério exige relatório de deploy antes do merge, mas a jornada coloca deploy depois do merge. Qual comportamento deve valer? (id=q1)
- [business] Nas entregas grandes, em que momento o aceite do owner deve bloquear o avanço? (id=q2)
- [business] Como distinguir uma entrega grande, que exige grill e aceite do owner, de um ajuste pequeno? (id=q3)
