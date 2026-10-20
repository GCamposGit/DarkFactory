# GRILL

## Premissas
- Esta etapa é somente de leitura e preparação do grill; não inclui alterações de arquivos nem retomada operacional.
- A causa permanece não comprovada: investigar propagação de project_id ao StageContext, consulta ao store e versão efetivamente carregada pelo worker antes de escolher a correção.
- runs.project_id é a identidade canônica do projeto; USR-315 e seu prefixo USR não devem ser usados para inferir projeto nem justificar fallback global para darkfac.
- O código de core/line/bindings.py contém fallback de contexto sem project_id para job_key.ticket_id; sua participação no incidente precisa ser reproduzida, não presumida.
- USR-315 consta como completed com evidência de entrega no backlog. A recuperação deve reconciliar essa entrega e preservar sua evidência, sem recriar tickets ou executar novamente trabalho já entregue.
- A validação deve cobrir contexto darkfac/USR-315, projeto distinto e identidade ausente ou desconhecida, preservando isolamento e falhando de forma estruturada quando não houver resolução segura.
- A implementação posterior deverá comprovar recuperação operacional com evidências do run real, respeitando leases e idempotência, além de passar pelo portão oficial sem relaxar cota ou Gate G1.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
