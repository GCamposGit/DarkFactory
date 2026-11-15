# GRILL

## Premissas
- Nenhuma pergunta bloqueante: retenção, limites de custo, canal de alertas e prazo de reteste por perfil já foram decididos pelo owner ou resolvidos nas premissas. Esta rodada não leu o código, então a escolha de componentes reaproveitados abaixo vem das premissas e deve ser conferida no planejamento.
- A demanda amplia os painéis existentes do DarkHub. Não cria um app novo nem telas fora dele, e a regra de negócio fica em core/ ou serviços, acessível headless. A UI só consome.
- Os painéis mostram projetos habilitados e bloqueados (com o motivo do bloqueio), cobertura, custo, pendências de reteste e evidências, sempre por projeto.
- Habilitação exige perfil e acesso explícitos. Estar no ProjectRegistry ou ser elegível ao BacklogFeeder não habilita nada. Projeto sem perfil, acesso, destino durável autorizado ou limite de custo por projeto fica bloqueado, com o motivo visível.
- O isolamento de evidência é aplicado no servidor, por identificador de projeto, em toda leitura (lista, detalhe, download e agregados). Filtro apenas na UI não conta. Cadastrar um projeto não cria permissão de leitura de evidência; a permissão é concedida à parte e explicitamente.
- O isolamento é provado por testes de comportamento: uma consulta com o contexto do projeto A não retorna evidência do projeto B, nem por id direto nem por agregação. Um teste desse tipo substitui testes de forma existentes, em vez de somar a eles.
- Cobertura distingue controles e dependências avaliados, desconhecidos, omitidos e com evidência vencida. Ausência de advisory ou falha de fonte aparece como desconhecido, nunca como sem vulnerabilidade.
- Evidência vencida pelo prazo do perfil perde validade e entra como pendência de reteste. Prazo ausente no perfil bloqueia o projeto.
- Custo e consumo são mostrados por projeto e por execução, com valores medidos separados de estimativas. A operação respeita o limite por projeto e os tetos globais existentes, e para ao atingir qualquer um deles.
- Alertas vão ao DarkHub e ao Telegram só para novidade acionável, falha ou prazo vencido. Alerta inalterado não se repete, com deduplicação persistente por chave de condição.
- Retenção das evidências e do histórico é permanente, usando o destino durável autorizado no perfil. Sem destino autorizado, nada daquele projeto é armazenado e ele fica bloqueado.
- A demonstração inicial é só em darkfac, com integração real e versões resolvidas em ambiente identificado. Nenhum segredo novo é necessário.
- A divergência de USR-449 e a ausência de QualityCycle e core/security nos caminhos inspecionados são tratadas na reconciliação do planejamento, sem concluir agora que o ticket não existe nem que o store está disponível.
- Mudança pontual de escopo médio, ampliando painéis existentes: não é produto novo inteiro.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
