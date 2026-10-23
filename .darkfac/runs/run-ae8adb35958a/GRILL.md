# GRILL

## Premissas
- É uma mudança pontual no DarkHub e na esteira existentes, mantendo Python, Pydantic v2, FastAPI na camada HTTP e regras de domínio em core/.
- O agente pode preparar e abrir o PR protegido como proposta; isso não autoriza publicação nem permite que ele emita sua própria aprovação.
- O clique exige identidade autenticada do owner e apresenta o diff do candidato, ticket, PR, caminhos protegidos exatos e SHA aprovado.
- O registro será publicado na base confiável por um componente autorizado pelo ato do owner, separado do agente de desenvolvimento; gravá-lo apenas no candidato não concede autorização.
- A autorização será vinculada ao commit SHA, sem aceitar identidade por tree hash como substituição; push, rebase ou resolução de conflito que altere o SHA exigirá nova aprovação.
- O portão oficial runner.py --quick será obrigatório em worker remoto sobre o candidato exato e limpo; indisponibilidade do worker manterá a publicação pendente, sem fallback local neste fluxo.
- Merge exige autorização válida, evidência do portão vinculada ao candidato e revisão independente conforme as regras existentes; deploy e acompanhamento continuam automáticos.
- Flag de ambiente isolada continuará insuficiente, com cobertura de teste; o guard e a proteção contra autoaprovação serão preservados.
- A OA só será concluída após confirmação da publicação, e o Hub distinguirá aprovação, espera pelo worker, falha do portão e publicação concluída. Nenhum comando Git será exigido do owner.

## Decisoes do owner
- (nenhuma pergunta bloqueante)

## Aguardando decisao do owner
- [business] Qual deve ser a validade da aprovação do owner para um SHA específico? O contrato atual de autorização exige expires_at. (id=q1)
- [intent] O novo fluxo deve também substituir os passos manuais das OAs protegidas já abertas, incluindo 041/045/046/050/052 quando ainda pendentes? (id=q2)
