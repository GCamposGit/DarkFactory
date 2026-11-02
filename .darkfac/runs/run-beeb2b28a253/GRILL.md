# GRILL

## Premissas
- Esta rodada limita-se à leitura local e ao grill, sem escrever arquivos, criar tickets, consultar serviços externos ou executar a prova operacional.
- A demanda evolui a fábrica existente em darkfac; não constitui produto novo nem amplia permissões de outros projetos.
- CloudCoordinator já integra FeederCycle, e BacklogFeeder utiliza ProjectRegistry e a fila canônica. O planejamento priorizará esses componentes e o control store existente antes de propor outro motor.
- A divergência de USR-449 permanece hipótese a investigar: esta leitura não verificou versão implantada, configuração efetiva nem store operacional. Ausência no checkout ou indisponibilidade não comprova inexistência do ticket.
- A prova deverá integrar os três domínios previstos nas premissas e acompanhar pelo menos um achado real não semeado pela fila canônica até correção e rechecagem persistida; testes sintéticos serão evidência complementar.
- A qualificação reutilizará políticas, limites e baselines verificáveis. Lacunas de cobertura, acesso, medição, cota ou capacidade terão causa explícita e não serão tratadas como sucesso.
- Concorrência, replay, retomada e queda de host serão exercitados em ambiente isolado, preservando leases, fencing e idempotência, sem indisponibilidade deliberada em produção.
- Desativar um domínio impedirá novas verificações e remediações originadas nele, preservando histórico e tickets já admitidos; rodadas atrasadas serão consolidadas com rastreabilidade e sem duplicações.
- O aceite dependerá de evidência vinculada ao candidato imutável: revisão de outra família, portão oficial com [HARNESS_PASS] e checagens executadas, verificações exigidas pelo CI, CI verde, merge confirmado, deploy, smoke e rechecagem real persistida.
- Sem achado real ou evidência suficiente, o aceite permanecerá pendente com motivo explícito. Manual, FASE_ATUAL e learning pack acompanharão a entrega; alterações protegidas exigirão autorização assinada.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
