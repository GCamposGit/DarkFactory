# GRILL

## Premissas
- A demanda é uma correção pontual no núcleo existente, sem novo scheduler e sem alterar piso de cota, G1 ou isolamento.
- O checkout já contém requeue_ticket(reopen=True), motivo obrigatório para reabrir completed, recusa de cancelled e uso pela convergência ao vivo; essa implementação será preservada e validada, sem duplicação.
- O despacho em CloudCoordinator já consulta capacidade local, mas implementa uma checagem própria sem chamar fleet_policy; o trabalho restante é integrar a política canônica ao caminho real de admissão.
- A capacidade medida por measure_workers será aplicada ao host local, limitada também por max_slots e pelo teto global existente; ela não será usada para inferir capacidade de hosts remotos.
- Capacidade local zero ou falha de medição impedirá novas admissões locais, preservando reservas e workers ocupados; hosts remotos continuarão sujeitos às verificações existentes.
- Reabertura será explícita, com motivo registrado e preservação do histórico anterior; evidência de entrega antiga não poderá continuar indicando conclusão válida.
- Os testes existentes de USR-321 e USR-337 serão aproveitados e complementados para verificar a chamada real à política, limites, ocupação e o caminho de falha de convergência.
- Esta etapa é exclusivamente de leitura: não serão escritos arquivos, executados tickets ou disparadas operações externas.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
