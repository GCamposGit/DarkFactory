# GRILL

## Premissas
- A demanda corresponde ao USR-328; esta etapa é somente leitura, sem executar Grok, alterar arquivos ou iniciar implementação.
- O diagnóstico deve comparar o caminho atual de leitura do Grok com os logs históricos, sem tratar a correção de transporte por --prompt-file do USR-322 como solução dos timeouts.
- O executor existente _run_bounded e seu encerramento de árvore devem ser reutilizados e verificados em Windows e Linux, incluindo processos descendentes e tempo total de limpeza.
- A evidência deve incluir duração medida por relógio monotônico, código de saída ou ausência explícita dele, stderr com segredos ocultados e resultado da verificação de processos remanescentes.
- O SHA histórico 8d413ed deve ser resolvido para o SHA completo; qualquer veredito vale somente para o candidato efetivamente examinado.
- A próxima rota deve ser calculada pelo roteador existente, respeitando família diferente do implementador, disponibilidade, cota e permissões de envio de código privado; a autorização histórica para Grok não autoriza automaticamente outro provedor.
- Timeout, resposta vazia e indisponibilidade produzem falha estruturada, nunca aprovação; se não houver rota elegível, registrar essa condição explicitamente.
- A causa raiz só será atribuída mediante reprodução e evidências; ausência de reprodução deve constar como limitação do diagnóstico.
- (technical) Qual limite usar no smoke diagnóstico e na coleta após encerrar a árvore de processos? -> **Smoke de 30 s e coleta adicional de até 10 s** (Permite detectar ausência de resposta com baixo custo; o executor já possui grace_s de 10 s. O prazo da revisão completa permanece configurável separadamente.)

## Decisoes do owner
- (nenhuma pergunta bloqueante)
