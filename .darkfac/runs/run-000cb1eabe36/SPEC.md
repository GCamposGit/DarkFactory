# SPEC

## Objetivo
Integrar a admissão real do CloudCoordinator à fleet_policy com capacidade local medida e validar a reabertura explícita já implementada para falhas de convergência ao vivo.

## Fora de escopo
- Criar scheduler paralelo.
- Alterar piso de cota, Gate G1, isolamento ou regras de elegibilidade remota.
- Inferir capacidade remota a partir da RAM local.
- Duplicar a implementação existente de requeue_ticket(reopen=True).

## Desenho
Adaptar candidatos, configuração e reservas do CloudCoordinator aos contratos da fleet_policy, usando select_host_for_ticket no caminho que efetivamente chama reserve_cycle. Informar verified_capacity somente para o host local, com valor obtido de measure_workers().workers; zero ou exceção bloqueiam novas admissões locais. Aplicar conjuntamente max_slots por host e teto global, contabilizando reservas existentes e admissões do ciclo. Preservar exclusividade de host ocupado, verificações remotas e reserva atômica; atualizar a seleção após conflitos sem exceder limites. Preservar requeue_ticket(reopen=True, reason=...) e seu uso atual pela convergência ao vivo, complementando a cobertura de histórico, invalidação de evidência e idempotência.

## Arquivos a tocar
- core/orchestrator/cloud_coordinator.py
- tests/test_usr337_dispatch_requeue.py
- tests/test_usr321_requeue_capacity.py
- tests/test_git_autonomy_live_proof.py

## Riscos
- O coordenador e a política usam representações distintas de configuração, candidatos e ocupação; a adaptação deve preservar prioridade e elegibilidade existentes.
- A medição é uma fotografia de capacidade; reservas atômicas e contabilização incremental continuam necessárias para impedir sobrealocação.
- Falha de medição atualmente permite admissão em modo não gerenciado; o comportamento local deverá passar a falhar fechado conforme o grill.
- A reabertura já existe: alterações desnecessárias no store ou na autonomia podem introduzir regressões.
