# GRILL

## Premissas
- Esta rodada foi só de leitura. Não consultei o código; baseei-me nas premissas e nas decisões do owner já registradas.
- O domínio é a avaliação contínua de vulnerabilidades de dependências, começando por darkfac. A demanda mede e apresenta a operação e não autoriza remediações nem campanhas novas.
- O custo e o consumo reutilizam o contador de orçamento do portfólio e o control store. Nenhum contador paralelo será criado.
- Cada registro por projeto e por execução tem um campo explícito `origem` (`medido` ou `estimado`). Valor medido vem de telemetria do provedor ou do harness. Valor estimado vem de tabela de preços ou de heurística e nunca é somado a medido sem rótulo.
- Harness de assinatura sem custo em moeda é registrado com consumo medido (tokens, janela de cota) e custo em moeda nulo ou estimado, nunca inventado.
- O limite de cada projeto vem do perfil e é obrigatório, conforme decisão do owner. Projeto sem limite fica bloqueado com motivo visível, e o teto global existente também vale.
- Ao atingir o limite de um projeto ou o teto global, a execução falha fechada: bloqueia novas execuções, registra o motivo e alerta o owner. Um limite mais alto só entra com decisão do owner.
- Como não há baseline, a demanda não introduz limiar de cobertura adicional. Só registra o medido e o compara com o limite autorizado.
- Comparação com o limite: uso medido, estimado, limite e percentual, separados por projeto e por execução.
- Alertas vão para o DarkHub e o Telegram quando houver novidade acionável, falha ou prazo vencido, sem repetir alerta inalterado. Isso inclui limite próximo, limite excedido e medição ausente.
- Retenção permanente no destino durável autorizado do perfil de cada projeto. Sem destino autorizado, nada é armazenado até o owner definir.
- O painel amplia o DarkHub existente, com isolamento por projeto e evidências restritas. Nenhum acesso, segredo ou dependência novos são necessários.
- Falha de fonte de medição aparece como medição ausente ou desconhecida, nunca como custo zero.
- Como em toda entrega da fábrica, a mudança atualiza o manual, `docs/FASE_ATUAL.md` e o learning pack. Os testes novos de comportamento substituem ou consolidam testes existentes.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
