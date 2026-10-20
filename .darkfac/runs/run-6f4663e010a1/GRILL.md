# GRILL

## Premissas
- Esta rodada é somente leitura; não executa a prova nem altera arquivos.
- OA-042 resolveu D1–D5; OA-043 e OA-047 continuam como dependências existentes, sem repetir perguntas sobre acessos.
- M1 e M2 serão reutilizados como antecedentes; suas evidências não substituem a execução real de M6.
- A prova usará clone novo de GCamposGit/pilot-hello fora de qualquer checkout DarkFac, fixado no SHA mergeado do produto a verificar.
- Serão usados Python 3.12+, venv exclusivo do produto e os comandos registrados setup, validate, build e smoke, incluindo geração de wheel.
- O executor deverá comprovar ambiente sem PYTHONPATH, VIRTUAL_ENV do core ou DARKFAC_*, além de isolamento dos serviços DarkFac sem interromper serviços compartilhados.
- O planejamento deverá localizar e verificar o script de prova mencionado na demanda; sua existência não foi confirmada nos arquivos consultados.
- O relatório JSON deverá registrar SHA completo, host, horário, comandos, resultados de instalação/testes/build/smoke, isolamento e varredura; verificações não executadas não contarão como sucesso.
- A escolha entre hosts já autorizados e o mecanismo de isolamento são decisões técnicas, sem necessidade de nova decisão de negócio.

## Decisoes do owner
- (nenhuma pergunta bloqueante)

## Aguardando decisao do owner
- [intent] A varredura deve permitir o texto aprovado 'DarkFac pilot' em strings da página e dos testes, exigindo ausência de dependências da fábrica? M1 registra esse texto em arquivos de código, mas M6 exige ausência literal de 'darkfac' em código executável. (id=q1)
