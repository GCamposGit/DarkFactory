# GRILL

## Premissas
- Esta rodada é exclusivamente de leitura e grill, sem criar arquivos, tickets ou executar alterações operacionais.
- A eficiência será um domínio do ciclo existente, reutilizando CloudCoordinator, FeederCycle, BacklogFeeder, ProjectRegistry, roteador e control store; um módulo adicional dependerá de necessidade técnica demonstrada.
- A consolidação diária será configurável, por projeto e ticket, com períodos em UTC, retomada de períodos atrasados e atualização diante de evidências tardias.
- ModelUsageLedger, ticket_cost, telemetria e estados da linha serão reconciliados por identificadores verificáveis; eventos espelhados não poderão duplicar custos ou tentativas.
- Custos de API, custo implícito de assinatura e consumo de cota manterão suas unidades e proveniência explícitas. Valores ausentes não serão tratados como custo zero conhecido.
- Falhas e tentativas contribuirão para custo e duração quando observáveis; retries serão distinguidos de chamadas de modelo, e latência de chamada será distinguida da duração da execução.
- O baseline usará histórico comparável e política configurada, registrando janela, amostras, método e versão. Sem política aplicável ou evidência suficiente, haverá limitação de cobertura, sem inventar limiar de negócio.
- Cada desvio qualificado admitirá um único ticket por projeto e identidade do problema, com amostras, baseline e comparação reproduzíveis; recorrências atualizarão a evidência e respeitarão os estados existentes.
- Mudanças nas fontes, política, baseline, estado do ticket e remediação invalidarão resultados afetados; a remediação exigirá rechecagem com evidência nova.
- Desativar o domínio por projeto impedirá novas admissões e remediações automáticas desse domínio, preservando histórico e rastreabilidade.
- A demonstração inicial será real em darkfac; cadastrar outro projeto não ampliará objetivos, acessos ou permissões.
- A divergência de USR-449 permanece com causa não confirmada e deverá ser reconciliada entre versão implantada, configuração e control store; indisponibilidade continuará distinta de ticket inexistente.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
