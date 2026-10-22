# GRILL

## Premissas
- Esta etapa é somente leitura: não altera arquivos, registra ações ou executa deploys.
- OA-044 está concluída; não é necessário repetir a configuração de registry, autoDeploy e agendamento.
- USR-339 já possui implementação parcial de GHCR, remoção de container_name e start-first; o planejamento deve completar e validar o existente.
- A espera pela imagem deve proteger tanto o CLI quanto o DokployDeploymentAdapter, sem fallback automático para build na VPS.
- A publicação do workflow protegido continua dependente da OA-052, ainda aberta; criar o registry na OA-044 não resolveu essa dependência.
- A imagem permanece privada, vinculada ao SHA completo; credenciais existentes devem ser verificadas sem expor segredos.
- A seção de agendamento já usa Register-ScheduledTask e pythonw.exe absoluto; deve ser conferida, preservando a sonda já agendada.
- O aceite exige medição externa a cada segundo durante toda a troca real, incluindo convergência ao SHA esperado; testes simulados não substituem essa evidência.
- Não aumentar VPS nem limites de recursos sem medição; conclusão exige também o portão oficial runner.py --quick.
- (technical) Qual estratégia usar para garantir a troca sem queda, considerando que ainda não há confirmação de Stack/Swarm no Dokploy? -> **Verificar o modo: start-first em Stack; blue/green em Compose** (A proposta prevê ambas as rotas; update_config sozinho não garante troca sem queda em Compose simples.)

## Decisoes do owner
- (nenhuma pergunta bloqueante)

## Aguardando decisao do owner
- [intent] “Um merge gera uma única imagem e um único deploy” vale também para merges apenas de documentação, testes ou ledger, hoje dispensados de redeploy? (id=q1)
