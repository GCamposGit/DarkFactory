# GRILL

## Premissas
- Esta rodada é somente leitura: não executa o ticket PH, não altera arquivos nem cria recursos.
- OA-042 resolveu D1–D5; permanecem pilot-hello, texto 'DarkFac pilot', Dokploy existente opção A, retenção arquivada e build no venv.
- M1 e M2 têm evidências em docs/evidence/USR-279-M1.md e docs/evidence/USR-279-M2.md; o planejamento verificará o estado atual e reutilizará os recursos existentes.
- O ticket PH será identificado pelo vínculo ao projeto pilot-hello e à demanda de M3; OA-047 acompanha a entrada real pelo Telegram, sem nova pergunta ao owner.
- A implementação ocorrerá em checkout próprio de GCamposGit/pilot-hello, com Python 3.12+, venv do produto e comandos setup, validate, build e smoke do descritor.
- A fábrica pode orquestrar a execução externamente; o produto não receberá runtime, imports, serviços nem portão de validação do core.
- A revisão independente deverá identificar implementador, revisor de outra família e SHA revisado; mudanças posteriores exigirão nova verificação das evidências afetadas.
- O merge squash dependerá de CI própria do produto verde para o SHA final do PR e ausência de conflito com main; serão registrados PR, SHA do PR, SHA do merge, link do run de CI e host executor.
- A limpeza abrangerá branch de trabalho local/remota e worktree temporária após confirmação do merge, preservando o checkout principal e as evidências.
- Deploy e smoke publicados pertencem a M5, a prova completa por clone limpo a M6 e o relatório consolidado a M7; OA-043 continua acompanhando acessos e configuração de deploy/webhook.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
