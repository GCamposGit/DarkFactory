# SPEC

## Objetivo
Impedir que decisões materiais pendentes sejam resolvidas por silêncio, prazo ou limite de exibição, mantendo a demanda em waiting_human até resposta explícita do owner.

## Fora de escopo
- Alterar AGENTS.md, governança protegida, permissões ou políticas de aceite.
- Modificar prazos de outros estágios ou reproduzir um incidente em produção.
- Desativar decisões técnicas reversíveis automáticas ou a política explícita de canário.
- Preencher segredos, contas ou acessos ausentes automaticamente.

## Desenho
Separar classificação de decisão do recorte de exibição: todas as perguntas materiais permanecem no estado pendente, enquanto cada rodada mostra no máximo três. Respostas parciais avançam para a rodada seguinte sem preencher recomendações ausentes. O vencimento do prazo apenas mantém waiting_human e pode gerar nova notificação; nunca cria escolha do owner. Perguntas técnicas reversíveis continuam automáticas com origem auto_technical, e somente execuções com política explícita de canário usam auto_canary. Os dois motores de grill devem emitir prontidão apenas quando não restar decisão material pendente.

## Arquivos a tocar
- core/line/stage_grill.py
- core/demands/grill.py
- core/demands/models.py
- core/demands/integrated_service.py
- tests/line/test_stage_grill.py
- tests/test_criar_mecanismo_de_grill_.py
- docs/manual/grill.md
- docs/FASE_ATUAL.md

## Riscos
- Clientes legados podem depender de auto_accept_unanswered ou das recomendações aplicadas silenciosamente.
- O controle de notified precisa ser reiniciado por rodada sem duplicar mensagens já entregues.
- Estados GRILL_PENDING.json criados por versões anteriores devem continuar legíveis e deixar de finalizar por prazo.
- A exceção de canário deve permanecer restrita à política explicitamente autorizada e nunca alcançar segredos ou acessos.
- Mudanças no contrato de GrillSession e no retorno integrado precisam manter compatibilidade de serialização e retomada do workflow.
