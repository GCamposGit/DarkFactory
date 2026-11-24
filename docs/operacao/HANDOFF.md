# Handoff operacional

Versao: 14
Atualizado em: 2026-11-24 (Claude Code, coordenador)

## Estado

- Fase: V0 da Dark Factory (ver `docs/FASE_ATUAL.md`). `core.line.v0_proof` em 0/5 (USR-557 em voo corrige o USR-279 marcado completed).
- `main` em `438c5ac8` (#946). Deploy de 24/11 em `darkfac-cloud`: coordinator e worker `running/healthy` na imagem `d6650ced` (#921). O #944 só muda testes. O `[CHECK-MAIN]` do deploy saiu `unknown` porque não houve CI concluído no main (USR-576).
- Despachante ativo. Em voo: USR-557 (Notebook). Ainda há 18 holds no recibo de coordenação.
- Hosts: Notebook e Desktop executam; o Grokbox entra e sai.
- Cotas (24/11 ~17h): Codex 26%, Grok 24%, Antigravity vencida (RPC). A leitura do Claude ficou desconhecida por 29 h (USR-573).

## Incidente de 24/11: main vermelho por 5 h sem alerta

O #934 (USR-561) passou a ignorar todo `.factory/runs/` e quebrou 4 testes de higiene no main em `2f6ada4f`. O pr-tests do PR passou porque os testes afetados não ligam o `.gitignore` aos testes que o leem (USR-575). As 10 execuções seguintes do CI no main terminaram canceladas e esconderam o vermelho (USR-576). O problema só apareceu no PR #912. A correção entrou pelo #944 (USR-574).

Ao assumir a coordenação, confira o último CI **concluído** do main: `gh run list --branch main --workflow "DarkFac CI" --limit 15`.

## Entregue em 24/11

- #921 (USR-538): `completed` só com prova `<sha>:merged` (e live quando exigido). Sem `gh`, a entrega recusa em vez de fazer push direto ao main. Conclusão legada sem merge volta para `planned`.
- #944 (USR-574): testes de higiene alinhados ao USR-561. Main verde de novo.

## PR #912 (USR-483) segurado

A revisão cruzada do Codex pediu mudanças de desenho (comentário no PR):

- lease em SQLite local, com dois PCs mantendo coordenadores válidos;
- despachante direto sai com 78 ou segue com lease vencida;
- sessões Claude compartilham o ID;
- Antigravity perde a preferência de `development`;
- segundo fluxo de coordenação paralelo a `core.ops.coordination`.

O próximo passo é redesenhar com um caminho único de coordenação e estado compartilhado entre hosts, não remendar. O CI do head `78780b51` está verde.

## Defeitos de 24/11

Regra do owner (24/11): corrija direto o que der e consolide o resto num ticket por sessão. Um ticket por sintoma vira loop de frota.

- Corrigidos direto no #946: USR-570 (identidade git de fallback em repo de produto), USR-571 (PEM sem corpo base64 não bloqueia; a chave real e a PEM criptografada continuam bloqueadas) e USR-575 (o fallback do PR mantém os testes citados por mudança não-Python, como `.gitignore`).
- Pendente num único ticket, o USR-572 (consolidado; o USR-573 e o USR-576 foram cancelados e absorvidos): (a) a retentativa abre worktree nova e cai em `build_evidence_mismatch`; (b) a CLI npm do Claude no Notebook rejeita o id Haiku, e a cota fica desconhecida; (c) o CI do main foi cancelado em série e escondeu o vermelho (o workflow é protegido).

## Armadilhas

1. Recusa não é falha, mas recusa repetida no mesmo sha é laço (USR-545).
2. Para fechar entrega feita fora do launcher, use `python -m core.demands.cli complete <TICKET> --sha <sha completo do origin/main> --evidence "..."`.
3. `learning.cli record-turn` altera o `learning_ledger.json` rastreado no checkout compartilhado. Antes de avançar o checkout, rode `git stash push -m <tag> -- .factory/learning/learning_ledger.json`, depois `merge --ff-only` e `stash apply <sha>`.
4. `dokploy_redeploy.py` sai com código 3 quando só o Notebook não converge. Confira as linhas `[RUNTIME IMAGE]`.
5. Id literal de modelo neste arquivo quebra o censo de modelos. Use alias.
6. A frota está rodando MDL-01..03 (Mansão do Lago, cliente) na branch `darkfac/m1-seo`. Nada vai para produção do cliente sem autorização expressa do owner.
