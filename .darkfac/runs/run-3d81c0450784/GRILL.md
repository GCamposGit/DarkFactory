# GRILL

## Premissas
- Esta rodada é exclusivamente de leitura; nenhum arquivo, ticket, acesso externo ou operação foi criado ou executado.
- O domínio considerado é a avaliação contínua de vulnerabilidades de dependências descrita nas premissas; a demanda amplia sua operação no portfólio, sem autorizar campanhas ofensivas ou remediações adicionais.
- Reutilizar CloudCoordinator, FeederCycle, BacklogFeeder, ProjectRegistry, roteador e control store; a elegibilidade do BacklogFeeder por cadastro não substitui habilitação explícita de perfil e acesso.
- QualityCycle e core/security não foram encontrados nos caminhos inspecionados; reconciliar a base entregue e as dependências anteriores antes de propor componentes novos.
- Projetos sem perfil, acesso ou parâmetros obrigatórios permanecem bloqueados, com motivo visível; não será necessário solicitar novos segredos nesta rodada.
- A demonstração inicial permanece em darkfac e exige versões resolvidas, ambiente identificado e integração real; requisitos declarados não comprovam versões instaladas.
- Cobertura distinguirá controles e dependências avaliados, desconhecidos, omitidos e evidências vencidas; ausência de advisory ou falha de fonte não será apresentada como ausência de vulnerabilidade.
- Custos observados e consumo serão apresentados por projeto e execução, distinguindo valores medidos de estimativas; limiares adicionais de cobertura dependerão de baseline e decisão material posterior.
- Permanecem as premissas de proveniência, aplicabilidade determinística, deduplicação persistente, reavaliação por eventos, retomada concorrente e rechecagem após correção.
- Os painéis ampliarão o DarkHub existente, com isolamento por projeto e evidências restritas; cadastro não concede novos acessos.
- A divergência de USR-449 permanece objeto de reconciliação futura, sem concluir nesta leitura que o ticket inexiste ou que o store está disponível.
- (technical) Como definir os prazos das provas periódicas de reteste? -> **Exigir prazo por perfil; evidência vencida perde validade e aparece como pendência** (A consolidação diária já prevista não define a validade da prova nem a periodicidade de reteste de cada projeto.)

## Decisoes do owner
- [business] Qual política de retenção deve valer para evidências e histórico operacional dos projetos habilitados? -> **Retenção permanente (sem expiração automática) para as evidências e o histórico operacional dos projetos habilitados. Usar o destino durável autorizado no perfil de cada projeto; se faltar um destino autorizado, aguardar a sua definição antes de armazenar dados desse projeto.** (respondida pelo owner)
- [business] Como autorizar os limites de custo e consumo desta operação? -> **Exigir limites por projeto e respeitar também os tetos globais existentes** (respondida pelo owner)
- [business] Onde e quando os alertas operacionais devem chegar ao owner? -> **DarkHub e Telegram: novidade acionável, falha ou prazo vencido, sem repetição inalterada** (respondida pelo owner)
