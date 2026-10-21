# DEMAND

- projeto: darkfac
- canal: line

## Texto original

# Diagnosticar timeouts sem veredito na revisao independente Grok

## Problema
USR-321 candidate8d413ed: revisao Grok autorizada pelo Owner nao emitiu veredito em tres execucoes limitadas. Logs duraveis na WTusr-321: review-usr321.json timeout362s apos ler SHA; review-usr321-final.json timeout302s semstdout; review-usr321-clean.json timeout182s semstdout/stderr mesmo com cwdtemporario, homeisolado semMCP, no-plan/tools/web/subagents. Nao confundir com WinError206 doUSR322. Gateoficial8dpass4839/44skip; mergeretido por ausencia de revisao.

## Jornada
Diagnosticar timeouts sem veredito na revisao independente Grok

## Fora de escopo
- Nao converter timeout em aprovacao nem permitir revisao da mesma familia.
- Nao assumir causa de rede, MCP ou quota sem reproduzir.

## Criterios de aceite
- Reproduzir com smoke curto e capturar duracao monotonic, codigo e stderr redigido.
- Uma revisao outrafamilia devolve veredito verificavel vinculado ao candidato ou falha estruturada com proxima rota elegivel.
- Deadline configurado termina arvore de processo e nao deixa execucao fantasma.
