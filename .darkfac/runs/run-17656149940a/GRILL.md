# GRILL

## Premissas
- Esta rodada é exclusivamente de leitura e grill, sem criar arquivos, tickets, executar testes ou acessar serviços externos.
- A demanda evolui a fábrica existente, com demonstração inicial em darkfac, sem criar produto novo ou ampliar permissões.
- A implementação priorizará CloudCoordinator, FeederCycle, BacklogFeeder, ProjectRegistry, roteador e control store; um módulo novo dependerá de lacuna técnica comprovada.
- A qualificação usará ambiente e persistência isolados, exercitando concorrência, replay, retomada e perda de host sem provocar indisponibilidade deliberada em produção.
- A matriz futura vinculará cenário, domínio, candidato, janelas abrangidas, resultado esperado e observado, evidências, custo medido e motivo de adiamento; medição ausente será explicitamente desconhecida.
- Os cenários abrangerão leases, fencing, idempotência, consolidação de janelas atrasadas, piso de cota, falta de capacidade e desativação de domínio, seguindo políticas existentes sem novos limiares.
- Desativação impedirá novas verificações e remediações originadas no domínio, preservando histórico e tickets já admitidos na fila canônica.
- Provas sintéticas de robustez e provas operacionais reais permanecerão identificadas separadamente; o aceite dependerá de pelo menos um achado real corrigido e rechecado com evidência persistida.
- A divergência de USR-449 continuará sem causa presumida até reconciliar versão implantada, configuração e store; indisponibilidade não provará inexistência do ticket.
- A entrega posterior incluirá os portões oficiais vinculados ao candidato, revisão de outra família, CI, merge, deploy, smoke, rechecagem real e atualização documental; ausência de evidência manterá o aceite pendente.

## Decisoes do owner
- [intent] Quais são os três domínios que a qualificação deve integrar? As premissas não os nomeiam e os componentes inspecionados não estabelecem essa tríade. -> **Segurança, qualidade de código e operação da fábrica** (respondida pelo owner)
