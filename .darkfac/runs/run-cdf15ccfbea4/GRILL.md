# GRILL

## Premissas
- Esta rodada é somente de leitura e preparação do grill; não executa o portão, altera arquivos ou publica o PR 821.
- A demanda é uma correção pontual do harness Python existente, sem decisão de negócio pendente ou necessidade demonstrada de novo segredo ou acesso.
- O diagnóstico deve distinguir capacidade insuficiente, testes lentos ou travados e falha de transporte por evidências; os sintomas relatados não confirmam a causa.
- A implementação deve reconciliar tickets e rotinas existentes de capacidade xdist, estabilidade e retomada remota, logs de timeout e tempos de execução antes de criar mecanismos novos.
- Somente hosts habilitados e elegíveis podem executar o portão, respeitando memória disponível e suite_lock; indisponibilidade de recurso apto deve gerar estado estruturado e retomável, sem falso PASS.
- O aceite exige portão oficial completo com HARNESS_PASS, descoberta e checagens positivas, preservando cache e vínculo verificável ao candidato efetivamente validado; o PR 821 permanece bloqueado até essa evidência.
- Não serão aumentados cegamente os timeouts, forçados workers além da memória, reduzidos testes ou alterados arquivos de governança protegida; a entrega deve atualizar manual e evidências.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
