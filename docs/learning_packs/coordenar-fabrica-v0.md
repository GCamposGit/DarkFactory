# Assumir uma sessão de coordenação

O operador pede “assuma a fábrica”. A sessão identifica o projeto, recupera o trabalho em voo e delega a próxima ação. Fechar a conversa permite que um run já admitido continue enquanto o serviço da frota estiver ativo. O modo de recuperação só admite as retomadas previstas na política vigente.

Um exemplo é a perda de leitura de cota durante o desenvolvimento de um produto. O coordenador solicita nova leitura e consulta o roteador. Se a falha persistir, vincula um reparo da DarkFac ao ticket afetado. O ticket de produto conserva sua etapa e seu projeto. A evidência da correção é uma leitura atual que o roteador consegue usar.

Para verificar o aprendizado entre sessões, localize no handoff a intervenção, o projeto, o harness e a evidência do run anterior. Ao assumir com outro harness, confira se o coordenador recuperou a causa e se um caso equivalente avança sem pedir ao operador a mesma intervenção.


## Atualização USR-539 — continuidade determinística

As entradas por pedido curto e pela leitura da skill convergem no módulo `core.ops.coordination`. O recibo é gravado antes do briefing e da leitura das lições. Se há run ativo, o coordenador recupera run_id, etapa e refs persistidas, e segura a admissão do ticket seguinte. A etapa roteada segue o status atual do ticket.

O diário de entrega registra ações/decisões do owner ligadas ao ticket (lista vazia significa zero intervenções), custo, digest candidato, SHA do gate, timestamps de merge/deploy e recibo. Falhas de runtime são observadas em `darkfac`, com run_id e evidência; falhas do produto ficam no projeto do ticket. O relatório de prova exige refs para arquivos existentes e não vazios.

**Mecânica:** a sessão de chat pode desaparecer, mas o recibo e o diário são estado durável; a próxima sessão retoma o mesmo run e só libera uma nova admissão depois da fronteira de deploy.
