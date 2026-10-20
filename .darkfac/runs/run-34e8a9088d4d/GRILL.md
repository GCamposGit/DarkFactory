# GRILL

## Premissas
- Esta rodada prepara somente o grill: não altera arquivos, envia mensagens, executa o piloto nem resolve ações humanas.
- OA-042 encerrou D1–D5; essas decisões serão reutilizadas sem novas perguntas, inclusive pelo Gate G1.
- M1 e M2 têm evidências em docs/evidence/USR-279-M1.md e docs/evidence/USR-279-M2.md; a execução verificará esses resultados sem recriar recursos.
- A entrada real será a mensagem do owner pelo Telegram prevista na OA-047, com projeto explícito: /demand pilot-hello: pagina inicial com o texto DarkFac pilot e endpoint /healthz que devolve status ok e a versao.
- O roteamento existente em core/intake/project_routing.py aceita projetos registrados explicitamente e encaminha projetos desconhecidos para onboarding, sem redirecioná-los silenciosamente para darkfac.
- O aceite de M3 exige evidência real correlacionando mensagem, demanda e ticket PH- no projeto pilot-hello, com horário e trecho do roteamento, além de verificar que nenhum ticket USR- foi criado para essa entrada.
- OA-047 está aberta no snapshot consultado e depende de OA-043; o estado operacional dessas dependências será verificado na execução, sem repetir pedidos de acesso ou configuração.
- OA-047 só será resolvida após comprovação dos critérios; entrada ausente ou falha manterá a ação aberta ou motivará sua reabertura com diagnóstico, sem declarar M3 concluído.
- O escopo desta demanda termina em M3; implementação, merge, deploy do produto e prova de independência permanecem nos marcos posteriores.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
