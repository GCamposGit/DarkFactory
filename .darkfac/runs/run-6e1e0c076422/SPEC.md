# SPEC

## Objetivo
Emitir aceite automático fail-closed somente após comprovar, para o mesmo candidato imutável, revisão cruzada com rechecagem real, todos os portões técnicos, CI e merge, deploy e smoke reais e fechamento documental completo.

## Fora de escopo
- Alterar regras de governança, padrões protegidos ou a política de autorização assinada.
- Ampliar permissões, projetos, destinos de deploy ou exceções de aceite comercial.
- Aceitar mocks, indisponibilidade, cobertura parcial ou adiamento como evidência positiva.
- Redesenhar os probes históricos de HF-15 ou a prova da V0 que não participem do fechamento de um ticket.

## Desenho
Reutilizar os artefatos tipados e o diretório durável de ticket-run. O verificador construirá um grafo de identidade entre três domínios: candidate_digest revisado, head/PR e merge SHA, e SHA do artefato observado em produção. Recibos estruturados registrarão revisão de outra família e rechecagem de achado, runner --quick com marcadores e contagem, verificações estáticas, CI, merge, deploy, smoke e documentação. O relatório final será persistido como pending ou accepted, sempre com motivos determinísticos; mudança de candidato ou vínculo incoerente invalidará apenas as evidências afetadas. Projetos comerciais continuarão exigindo o recibo do owner e alterações protegidas continuarão exigindo autorização assinada.

## Arquivos a tocar
- core/line/stage_review.py
- core/line/stages.py
- core/line/ticket_engine.py
- core/line/ticket_extras.py
- core/harness/markers.py
- core/git/autonomy.py
- run_ticket.py
- core/line/fleet_dispatcher.py
- scripts/dokploy_redeploy.py
- docs/manual/motor.md
- docs/manual/merge_autonomo.md
- docs/FASE_ATUAL.md

## Riscos
- O squash impede comparar diretamente candidate_digest e merge SHA; o vínculo deve usar head do PR, recibo de merge e prova de ancestralidade.
- Deploys em lote podem publicar um SHA posterior ao merge do ticket; o verificador deve exigir prova de que o merge é ancestral do artefato observado.
- Artefatos pós-merge precisam sobreviver à remoção da worktree e a retomadas idempotentes.
- Uma aprovação direta sem achado real não satisfaz a premissa de rechecagem e deve permanecer pendente.
- Falhas ou indisponibilidade de GitHub, Dokploy, smoke ou control store não podem produzir aceite por omissão.
- O estado atual pode registrar conclusão antes do deploy; o novo aceite deve permanecer separado e pendente até o fechamento operacional.
