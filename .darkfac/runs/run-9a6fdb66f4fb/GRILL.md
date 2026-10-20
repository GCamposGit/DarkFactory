# GRILL

## Premissas
- A demanda é uma correção pontual da esteira existente, sem criação de produto ou mudança de regras de negócio.
- runs.project_id deve determinar o projeto; USR-315 é identidade de ticket e não deve virar projeto nem justificar fallback para darkfac quando o lookup falhar.
- A causa permanece não comprovada: a investigação deve comparar versão do worker, lookup do store e propagação de StageContext.project_id. Já existem correção e testes desse contrato associados a USR-303.
- USR-315 consta como completed no backlog consultado. A prova operacional deve verificar a entrega antes de qualquer retomada e impedir nova implementação de ticket já entregue; um run obsoleto deve ser reconciliado preservando seu histórico.
- A validação deve cobrir planning com project_id darkfac e ticket USR-315, falha de lookup, projeto não registrado e isolamento entre projetos, além do portão oficial na implementação.
- Esta etapa limita-se à leitura e ao grill; não altera arquivos, retoma runs ou modifica serviços. Piso de cota, Gate G1 e isolamento de projetos permanecem obrigatórios.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
