# SPEC

## Objetivo
Diagnosticar os timeouts de revisão Grok do USR-328 com evidência verificável, encerramento limitado de processos e resultado independente vinculado ao candidato.

## Fora de escopo
- Executar Grok, escrever arquivos ou implementar nesta etapa de planejamento.
- Converter timeout, saída vazia ou indisponibilidade em aprovação.
- Permitir revisão pela mesma família do implementador.
- Tratar o transporte --prompt-file do USR-322 como solução comprovada dos timeouts.
- Atribuir causa a rede, MCP ou cota sem reprodução.
- Enviar código privado a outro provedor sem autorização aplicável.

## Desenho
Reutilizar _run_bounded e o caminho atual de leitura Grok. Instrumentar resultado e limpeza com relógio monotônico, preservando código de saída real ou ausência explícita, stderr redigido e verificação de descendentes. Disponibilizar diagnóstico explícito com smoke de 30 s e orçamento adicional total de até 10 s para encerramento e coleta; a revisão completa mantém deadline configurável separado. Comparar evidências atuais aos três logs históricos, distinguindo fatos observados de hipóteses. Resolver 8d413ed para SHA completo antes de associar qualquer revisão histórica; o objeto não foi localizado neste checkout, cuja baseline é 9033623f8f2411224631fc9ddcc59d15466f58f8. Integrar falhas estruturadas ao estágio de revisão e consultar o roteador existente para próxima rota elegível, sem contornar autorização, disponibilidade, cota ou independência de família.

## Arquivos a tocar
- core/line/agent_cli.py
- core/line/review_diagnostics.py (novo)
- core/line/stage_review.py
- tests/line/test_bounded_runner.py
- tests/line/test_review_diagnostics.py (novo)
- tests/line/test_stage_review.py
- docs/runbooks/grok_review_timeouts.md (novo)

## Riscos
- Logs históricos da WTusr-321 e objeto Git 8d413ed podem não estar acessíveis; ausência deve ser registrada sem inventar conteúdo ou identidade.
- Smoke de 30 s pode não reproduzir timeouts históricos de 182–362 s; não reprodução limita o diagnóstico.
- Encerramento e herança de pipes diferem entre Windows e Linux; mocks não substituem testes reais nos dois sistemas.
- Instrumentação pode expor segredos em stderr ou argumentos se a redação não ocorrer antes da persistência.
- Uma rota tecnicamente disponível pode não ter autorização para receber código privado.
- Mudanças no executor compartilhado podem afetar outros harnesses e cancelamento.
