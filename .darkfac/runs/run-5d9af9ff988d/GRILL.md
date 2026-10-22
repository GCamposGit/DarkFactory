# GRILL

## Premissas
- Esta etapa é somente leitura: nenhuma alteração de arquivo, chamada de modelo ou despesa será realizada.
- Reutilizar a bateria determinística existente em core/models/qualification.py, com HTTP falso nos testes e evidências por candidato.
- Ler autorização e tetos do registro; autorização ausente, chave ausente, preço desconhecido ou custo máximo não calculável impedem chamadas reais.
- Consultar credenciais exclusivamente no ambiente ou mecanismo seguro existente, sem solicitar valores secretos no Grill nem registrá-los em arquivos versionados.
- O teto diário será agregado entre candidatos, processos e hosts da fábrica, por dia UTC, incluindo novas tentativas; reservas persistentes impedirão gasto duplicado após falhas ou reinícios.
- Reservar o custo máximo antes de cada chamada, limitando tokens e contabilizando cobranças adicionais; timeout com cobrança incerta não libera automaticamente a reserva.
- Preencher preços ausentes de candidatos novos e já cadastrados com identificação exata, unidade USD por milhão de tokens, fonte e data; correspondências ambíguas continuam no Grill.
- Processar também candidatos existentes ainda sem qualificação, respeitando orçamento e idempotência, pois o gancho atual só recebe os recém-descobertos.
- Preservar o piso de cota de 15%, os modelos proibidos, o isolamento em worktree e os portões existentes de promoção.
- As decisões deste Grill deverão ser persistidas pela etapa responsável antes de habilitar gasto real.
- (technical) Quando só houver preço do OpenRouter, como tratar uma qualificação executada diretamente no provedor? -> **Exigir tarifa aplicável ao canal; sem correspondência comprovada, bloquear e enviar ao Grill** (O teto precisa ser verificado antes da chamada. Uma tarifa de gateway não garante o custo da API direta, especialmente com cobranças adicionais ou tokens de raciocínio.)

## Decisoes do owner
- (nenhuma pergunta bloqueante)

## Aguardando decisao do owner
- [business] Quais tetos de gasto em USD você autoriza para qualificação, por candidato e no total diário da fábrica? (id=q1)
- [business] Quais canais de cobrança ficam autorizados nesta primeira entrega? (id=q2)
- [business] A autorização deve permitir qualificação automática de candidatos elegíveis ou exigir aprovação individual antes de cada bateria? (id=q3)
