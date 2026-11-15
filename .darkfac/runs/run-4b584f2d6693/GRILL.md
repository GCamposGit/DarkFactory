# GRILL

## Premissas
- Esta rodada é só de leitura. Nenhum arquivo, ticket ou acesso externo foi criado ou executado.
- Retenção permanente, limites de custo por projeto junto com os tetos globais, e alertas no DarkHub e no Telegram sem repetição inalterada já foram decididos pelo owner e não são reabertos.
- Prova vencida perde a validade e aparece como pendência, conforme a decisão técnica já registrada.
- Reaproveitar CloudCoordinator, FeederCycle, BacklogFeeder, ProjectRegistry, o roteador e o control store. Não criar componentes paralelos antes de reconciliar a base entregue, porque QualityCycle e core/security não foram encontrados.
- A demonstração é só no darkfac, com integração real, versões resolvidas (não apenas requeridas) e ambiente identificado. Mock não conta como entrega.
- Falha de fonte ou ausência de advisory aparece como desconhecido, nunca como ausência de vulnerabilidade.
- Retomada concorrente, deduplicação persistente, reavaliação por evento e rechecagem após correção seguem as premissas já registradas, com mecanismos determinísticos.
- Projetos sem perfil, acesso ou parâmetros ficam bloqueados com motivo visível. Nenhum segredo novo é pedido nesta rodada.
- Os painéis ampliam o DarkHub existente, com isolamento por projeto.

## Decisoes do owner
- (nenhuma pergunta bloqueante)

## Aguardando decisao do owner
- [business] Quais prazos de reteste por perfil devem valer para o perfil do darkfac na demonstração, e qual o prazo padrão para perfis sem valor definido? (id=q1)
- [account] Qual é o destino durável autorizado para registrar as provas do darkfac na demonstração? (id=q2)
