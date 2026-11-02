# GRILL

## Premissas
- Esta rodada permanece exclusivamente de leitura e grill, sem criar arquivos, tickets ou executar operações externas.
- A demanda é uma evolução da fábrica existente, com demonstração inicial em darkfac; não cria um produto novo nem amplia permissões de outros projetos.
- A implementação reutilizará CloudCoordinator, sua integração com FeederCycle, BacklogFeeder, ProjectRegistry, roteador e control store. Um novo quality_cycle.py dependerá de necessidade comprovada na inspeção técnica.
- A prova integrará os três domínios e acompanhará pelo menos um achado real até a rechecagem persistida após correção; cenários sintéticos de robustez não substituirão esse achado nem a evidência operacional real.
- Concorrência, replay, retomada e queda de host serão exercitados em ambiente isolado de qualificação, preservando leases, fencing e idempotência, sem provocar indisponibilidade deliberada em produção.
- A evidência distinguirá cobertura realizada, cobertura insuficiente, domínio desativado, indisponibilidade, adiamento por cota ou capacidade e custo observado; ausência de medição não será apresentada como custo zero ou sucesso.
- Rodadas atrasadas serão consolidadas sem duplicar achados ou remediações, preservando as janelas abrangidas e a rastreabilidade das evidências.
- A divergência relatada para USR-449 será investigada reconciliando versão implantada, configuração e store; a causa ainda não está confirmada e indisponibilidade não equivale a ticket inexistente.
- A qualificação seguirá as políticas e baselines verificáveis existentes, sem inventar limiares de negócio. Ausência de achado real ou evidência suficiente manterá o aceite pendente com motivo explícito.
- Desativar um domínio impedirá novas verificações e remediações originadas nele, preservando histórico e evidências; não implicará cancelar automaticamente tickets já admitidos na fila canônica.
- O roadmap será dividido em unidades pequenas com dependências explícitas, respeitando capacidade elegível, orçamento e piso de cota, sem estabelecer prazos novos.
- O aceite automático exigirá evidências vinculadas ao candidato imutável: revisão de outra família, runner --quick com [HARNESS_PASS] e checagens executadas, lint-imports, complexity_ratchet, docs_sync, CI verde, merge confirmado, deploy, smoke e rechecagem real persistida.
- Manual, FASE_ATUAL e learning pack integrarão a entrega; arquivos protegidos continuarão sujeitos à autorização assinada vigente.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
