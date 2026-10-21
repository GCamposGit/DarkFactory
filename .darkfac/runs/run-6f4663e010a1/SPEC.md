# SPEC

## Objetivo
Executar a prova de independencia do produto piloto em clone limpo fora de qualquer checkout DarkFac, usando o script de verificacao da fabrica, e anexar relatorio JSON com SHA verificado como evidencia do marco M6.

## Fora de escopo
- Alterar codigo do produto piloto ou da fabrica
- Repetir decisoes de acesso de OA-043/OA-047
- Substituir evidencias de M1/M2 pela execucao real de M6
- Interromper servicos DarkFac compartilhados
- Criar novas telas ou fluxos de produto

## Desenho
Localizar/confirmar o script de prova da fabrica e o SHA mergeado do produto piloto. Em host autorizado, clonar GCamposGit/pilot-hello em diretorio fora de qualquer checkout DarkFac, fixar o SHA, criar venv exclusivo com Python 3.12+, executar setup/validate/build (incluindo wheel) e smoke com ambiente sanitizado (sem PYTHONPATH, VIRTUAL_ENV do core ou DARKFAC_*) e sem servico DarkFac acessivel. Rodar varredura permitindo apenas o texto aprovado 'DarkFac pilot' em strings de pagina/testes, proibindo imports, caminhos e chamadas da fabrica. Emitir relatorio JSON com SHA completo, host, horario, comandos, resultados, isolamento e varredura; verificacoes nao executadas nao contam como sucesso.

## Arquivos a tocar
- scripts/verify_independence.py (ou script equivalente da fabrica, a localizar)
- relatorio JSON de evidencia do M6 (artefato gerado, nao versionado no produto)
- documentacao/evidencia do marco M6

## Riscos
- Script de prova pode nao existir ou divergir da demanda
- Ambiente do host pode vazar PYTHONPATH/VIRTUAL_ENV/DARKFAC_* e invalidar a prova
- Isolamento de servicos DarkFac pode afetar servicos compartilhados se mal configurado
- Varredura pode gerar falso positivo/negativo quanto ao texto aprovado 'DarkFac pilot'
- SHA mergeado pode mudar ou nao estar disponivel no momento da execucao
