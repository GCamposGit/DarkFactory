# USR-328 - Timeouts sem veredito na revisao independente Grok

## Evidencia observada (21/10/2026)
- Smoke minimo (`.factory/reports/grok-review-smoke-20261021.json`): prompt curto, `returncode=0`, `duration_s=6.5`,
  stdout `OK`. stderr: `Resident session actor exited unexpectedly; reaping as DeadFailed`. O CLI funciona em prompt trivial.
- Revisoes reais USR-321 e USR-322 (`grok-reviews-321-322/`): `timed_out=true`, `returncode=1`, `duration_s=182.7`
  (deadline 180 s), `text=""`. USR-321 stderr: `Failed to fetch models ... cli-chat-proxy.grok.com/v1/models ... TimedOut`,
  fallback grok-4.7 -> grok-4.6. USR-322 stderr vazio. No ticket: 362 s, 302 s e 182 s sem stdout.
- Conclusao: o timeout ocorre so com o pacote de revisao grande; prompt trivial responde em 6,5 s. O stderr mostra
  falha de rede ao listar modelos em UMA execucao, mas nao em outra, entao rede/MCP/cota NAO foram provadas como causa
  (nao-objetivo respeitado). Causa raiz do atraso do CLI permanece nao reproduzida; o defeito corrigido e o tratamento.

## Lacunas no codigo
1. `_kill_process_tree` nao verificava se a arvore realmente morreu (possivel execucao fantasma).
2. A revisao tratava timeout como erro generico (`review_agent_timeout`), sem duracao nem proxima rota, e podia
   reeleger o mesmo harness.

## Correcoes
- `core/line/agent_cli.py`: `_kill_process_tree` agora captura os descendentes antes, mata, verifica sobreviventes (psutil)
  e os mata de novo; devolve a lista de PIDs sobreviventes (log de erro se restarem).
- `core/line/stage_review.py`: timeout vira `StageResult(retry, cause_code="review_timeout:harness=H:duration_s=D:next_route=X|none")`,
  nunca aprovacao; o harness que estourou entra em `ReviewState.timed_out_harnesses` e e excluido do proximo `pick`
  (o roteador mantem a regra de outra familia que o implementador).

## Testes
`tests/test_usr328_review_timeout.py`: CLI falso lento com neto; deadline mata a arvore sem fantasma; timeout de revisao e
falha estruturada, sem aprovacao, com proxima rota de outra familia e sem reeleger o harness.
