# GRILL

## Premissas
- A mudança abrange workers de teste em core/harness/remote_worker.py e seu dispatcher, com compatibilidade Windows e Linux.
- Os 10 minutos representam uma janela contínua com CPU abaixo do limiar e ausência de nova saída do job; atividade real reinicia a contagem.
- Mensagens do próprio watchdog e consultas de saúde não contam como progresso do job.
- Reiniciar significa relançar o daemon do worker pelo mecanismo existente, preservando host, porta e node_id; não reiniciar a máquina nem executar atualização de código.
- O job perdido deve manter registro e logs persistidos, com erro de infraestrutura reconhecido pelo dispatcher, sem produzir veredito de teste.
- Telemetria e alerta usarão as integrações existentes para operações; falha no Telegram não deve impedir a recuperação.
- A implementação deverá validar recuperação, preservação de jobs ativos e reenvio sem duplicidade, além de passar pelo portão oficial python core/harness/runner.py --quick.
- Esta rodada é somente de leitura; nenhum arquivo foi alterado e nenhum teste ou reinício foi executado.
- (technical) Como medir CPU < 5% para detectar a estagnação? -> **CPU agregada do runner e descendentes, com 100% equivalente a um núcleo** (O worker já acompanha a árvore de processos; CPU global pode ocultar travamento ou diluir trabalho ativo em máquinas com muitos núcleos.)
- (technical) Qual deve ser a política de reenvio após a recuperação automática? -> **Reutilizar limites, idempotência e fallback atuais do dispatcher** (remote_dispatch.py já trata perda por reinício e emite [REMOTE_RETRY]; reutilizar esse caminho evita repetição ilimitada e execuções duplicadas.)

## Decisoes do owner
- (nenhuma pergunta bloqueante)

## Aguardando decisao do owner
- [intent] CPU baixa e ausência de logs também podem ocorrer durante espera legítima por lock ou I/O. Nessas situações, o limite de 10 minutos deve provocar recuperação? (id=q1)
