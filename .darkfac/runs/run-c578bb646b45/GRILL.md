# GRILL

## Premissas
- Esta etapa é somente leitura: não haverá implementação, execução de testes, alterações de arquivos ou ações nos serviços.
- A mudança abrange o protocolo de jobs de teste em core/harness/remote_worker.py e core/harness/remote_dispatch.py, com compatibilidade Windows e Linux.
- Os 10 minutos representam uma janela contínua sem nova saída do job e com CPU abaixo do limiar; atividade válida reinicia a contagem. Logs sintéticos do watchdog não contam como progresso.
- A recuperação preservará logs e causa estruturada de perda antes de encerrar a árvore de processos; perda de infraestrutura não será apresentada como reprovação dos testes.
- O dispatcher reutilizará o mecanismo existente de [REMOTE_RETRY], com duas retentativas no mesmo worker por padrão e posterior tentativa nos demais workers; as configurações existentes de fallback local e execução remota obrigatória serão preservadas.
- O reinício será do processo do worker, sem reboot da máquina nem atualização automática do código via /system/update.
- Telemetria e alerta usarão os mecanismos existentes e o destino Telegram ops já configurado, sem solicitar novos segredos nesta etapa; falha no envio do alerta não impedirá a recuperação.
- A implementação deverá validar recuperação, preservação de jobs com progresso, retentativas limitadas e aprovação no portão oficial python core/harness/runner.py --quick.
- (technical) Como medir o limiar de CPU < 5% para detectar o job parado? -> **CPU agregada do job e descendentes, com 100% por núcleo** (A árvore de processos já é rastreada pelo worker. A média da máquina pode esconder um teste ativo em um único núcleo ou mascarar travamento com carga externa.)
- (technical) Se reiniciar o daemon puder interromper outra execução saudável atendida pelo mesmo worker, qual comportamento deve prevalecer? -> **Encerrar o job perdido e adiar restart até não haver execução saudável** (O worker também expõe execução de comandos e harnesses; preservar trabalho saudável atende ao requisito de não matar jobs com progresso real.)

## Decisoes do owner
- (nenhuma pergunta bloqueante)

## Aguardando decisao do owner
- [intent] CPU baixa e ausência de logs também podem ocorrer em esperas legítimas de I/O ou pelo suite_lock. Essas esperas devem impedir a recuperação automática? (id=q1)
