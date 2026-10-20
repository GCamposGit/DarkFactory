# GRILL

## Premissas
- Esta rodada é somente leitura: não altera arquivos, registra ações, executa retenção nem marca critérios como concluídos.
- OA-042 já resolveu D1–D5; D4 permanece manter arquivado, sem nova consulta ao owner.
- M1 e M2 serão reutilizados a partir de docs/evidence/USR-279-M1.md e docs/evidence/USR-279-M2.md, verificando sua correspondência com os registros atuais.
- M7 depende de evidências verificáveis de M3–M6; a ausência dessas evidências impede declarar o piloto concluído.
- O relatório consolidará a linha do tempo M1–M6, tickets, revisão independente, SHAs de PR e merge, CI, deploy, smoke e prova de independência, distinguindo evidência confirmada de pendência.
- As intervenções manuais serão contadas por ação humana efetivamente realizada e documentada, com finalidade, momento e referência; ações pendentes serão apresentadas separadamente.
- Cada falha identificada no percurso deverá ter vínculo com commit/PR de correção ou ticket formal, sem apresentar diagnóstico isolado como resolução.
- Na execução posterior, D4 será comprovada como executada ou registrada no backlog de ações do owner com instruções e critério de verificação; registrar a ação não equivale a comprovar arquivamento.
- Os critérios 2 e 3 do USR-279 somente serão marcados mediante evidências reais da cadeia completa e da independência do produto, respectivamente.
- OA-043 e OA-047 permanecem dependências existentes; não serão solicitados novamente acessos, segredos ou decisões já acompanhados por essas ações.
- A prova de independência usará clone separado, instalação, validate, wheel e execução sem checkout, imports ou serviços DarkFac; permanece permitido o texto de produto 'DarkFac pilot' decidido em D2.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
