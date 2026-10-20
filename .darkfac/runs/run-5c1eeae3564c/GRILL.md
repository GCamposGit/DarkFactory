# GRILL

## Premissas
- A demanda é uma correção pontual no worker de testes do grokbox, sem instalar systemd nem substituir a supervisão existente por outro serviço concorrente.
- A baseline já contém scripts/grokbox/grokbox-supervisor.sh, grokbox-common.sh e referências no runbook, identificados como USR-315; o planejamento deve aproveitar essa implementação e verificar as lacunas restantes.
- O limite de 60 segundos abrange a mudança de HEAD até a nova instância responder em /health quando o worker estiver ocioso; se estiver ocupado, a atualização fica pendente até terminar o trabalho.
- Ociosidade exige busy=false, fila vazia e ausência de execuções ativas; uma falha ao consultar a saúde não constitui autorização para reiniciar por atualização.
- Como /health.git_sha atualmente consulta o HEAD a cada requisição, o SHA novo sozinho não comprova recarga: a verificação também deve demonstrar mudança de instance_id e vínculo da nova instância ao commit carregado.
- Os testes devem executar a lógica de decisão com estados e tempo controlados, cobrindo HEAD igual, HEAD diferente com worker ocioso e atualização adiada durante trabalho; os testes atuais de inspeção textual não substituem essa cobertura.
- Esta rodada é exclusivamente de leitura e esclarecimento; não inclui alterações de arquivos, instalação na VM ou execução do ciclo de desenvolvimento.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
