# Pós-V0: fechar a fila existente

## Decisões do owner

O V0 foi encerrado em 16/10/2026, com cinco entregas integradas e operação
verificada em `d465e2e4377e47cf5eb1ac3ac14aa20fc8b5df02`. Falhas recuperáveis e
resgates fazem parte da operação; não reiniciam o progresso de entregas aceitas.

Nesta sessão o owner priorizou autonomia para demandas de projetos independentes.
As respostas registradas em OA-024 e OA-025 autorizam um piloto técnico pequeno e
descartável, repositórios privados na conta GitHub do owner e ambientes separados
na infraestrutura existente, sem novas contratações. A autorização permite preparar
o piloto; não comprova que o fluxo independente já funciona.

A orientação posterior do owner é a prioridade vigente: **finalizar os tickets
abertos, sem criar funcionalidades novas salvo necessidade estrita**. O piloto
aguarda a resolução dos bloqueios existentes. Não há descongelamento geral do
backlog nem autorização para misturar o produto do piloto ao núcleo compartilhado.

## Lote em implementação

| Ticket | Defeito reproduzido | Resultado exigido |
| --- | --- | --- |
| USR-273 | Atualizar cadastro pela CLI apagava comandos e configuração de entrega | Importação completa/parcial preserva campos omitidos; entrada inválida não altera o cadastro |
| USR-276 | Launcher de projeto externo pulava o build declarado | Validar, construir e executar smoke na ordem; falha impede publicação |

São correções necessárias dos contratos existentes, com testes de regressão que
falham antes da mudança. A entrega exige revisão independente, portão oficial,
CI no candidato, merge e deploy. O projeto externo usa seus próprios comandos de
aceite; a suíte do núcleo verifica apenas o comportamento da fábrica.

## Ordem após o lote

1. Auditar e concluir USR-194 e USR-195: pedido de decisão chega ao Hub e a resposta
   fica durável no ticket, permitindo retomada sem trabalho manual de sincronização.
2. Resolver bloqueios já abertos de execução e validação: USR-270, USR-268,
   USR-272 e USR-253, preservando o Grokbox como worker primário e o fallback.
3. Trabalhar a fila existente por dependências e impacto na autonomia, incluindo
   USR-211 e USR-221. Itens com mais de três critérios são delimitados antes de
   execução; não se acrescentam objetivos ao ticket durante a implementação.

USR-274, USR-275 e USR-278 registram lacunas observadas no onboarding, ambiente e
intake de projetos externos. USR-279 registra a prova técnica solicitada. Permanecem
em `later`, sem execução neste lote; só entram antes da fila se forem bloqueios
estritamente necessários para concluir um ticket existente. O registro preserva a
evidência sem transformar a auditoria em expansão automática de escopo.

## Prova futura de independência

Quando os bloqueios existentes estiverem resolvidos, o piloto deve poder ser
clonado, instalado, testado, construído e iniciado sem checkout nem serviço DarkFac.
Terá repositório, ambiente e testes próprios. As evidências devem vincular demanda,
commit, testes, ambiente e smoke. Ausência de credencial ou decisão material gera
ação humana registrada; falhas técnicas são tratadas pela linha.
