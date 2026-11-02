# DEMAND

- projeto: darkfac
- canal: line

## Texto original

# Qualidade continua 1/3: ciclo recorrente comum com documentacao, seguranca e eficiencia

## Problema
Objetivo aprovado pelo owner: a fabrica e seus projetos precisam descobrir e encaminhar melhorias rotineiramente sem pedido manual, usando plenamente capacidade elegivel dentro dos tetos atuais. Ha coordenador, feeder, cadastro, fila persistente, roteador, canario, sentinela, docs-sync e retrospectiva; falta liga-los em um ciclo reutilizavel de observar, qualificar, priorizar, remediar e medir. Reconciliar baseline operacional antes de alterar e reutilizar implementacoes atuais; o caminho sugerido quality_cycle.py so nasce se a funcao nao existir de forma reaproveitavel. Entregar dominio headless com politica por projeto, disparo por mudanca e vencimento de evidencia, execucao devida persistida, cobertura explicita, retomada, cache e deduplicacao. Conectar primeiro documentacao, avisos de seguranca aplicaveis e telemetria de eficiencia. Cadencias iniciais tecnicas: consolidacao diaria barata de avisos/telemetria e revisao semanal de lacunas documentais, alem de eventos relevantes; configuraveis e ajustadas por evidencia. Essas cadencias operacionais nao sao prazo de implantacao. Achados aptos entram na mesma fila com origem e criterios verificaveis; correcoes pequenas dentro de objetivos aprovados usam a autonomia atual. Acionamento recorrente nao autoriza gasto novo, acesso novo ou mudanca de objetivos. Aceite desta entrega: automatico pelo fluxo normal quando criterios abaixo e prova real estiverem atendidos. Sem espera artificial para trabalho elegivel; usar slots e hosts aptos existentes e respeitar piso de cota. Pesquisa primaria: https://www.anthropic.com/research/building-effective-agents ; https://sre.google/workbook/eliminating-toil/ ; https://csrc.nist.gov/projects/ssdf/publications ; https://diataxis.fr/ . Handoff autocontido: este texto, fronteiras e criterios sao suficientes para o worker, sem depender dos arquivos locais nao publicados da conversa.

Politica de validacao e entrega: ["Executar os comandos na worktree imutavel do candidato, nunca no checkout compartilhado.", "python -m core.harness.runner --quick", "lint-imports", "python scripts/complexity_ratchet.py", "python -m core.harness.docs_sync --base origin/main --head HEAD", "Revisao por familia diferente, CI verde, merge, deploy, smoke e evidencia real conforme fluxo vigente."]
Diretriz do owner: Abrir os tickets no roadmap de implantacao e deixar a esteira em andamento assumir. Utilizar capacidade maxima elegivel dentro do orcamento vigente para avancar o mais rapido possivel, sem prazos estimados no plano.

Baseline observado: Hub nao encontra ticket presente na fila persistente: GET https://darkhub.ggcampos.com/api/demands/tickets/USR-449 retornou HTTP 404 com detail Demand ticket USR-449 not found, enquanto get_ticket no PostgreSQL retorna a linha planned e o BacklogFeeder a considera elegivel. Mesmo resultado de leitura publica para os demais tickets desta implantacao. Causa ainda nao confirmada; reconciliar fonte/configuracao e versao do Hub antes de criar adapter ou fila nova. Este defeito integra a reconciliacao operacional desta entrega.

## Jornada
Coordenador identifica rotina devida ou insumo alterado de projeto cadastrado.
Coleta evidencias deterministicas e consulta o modelo apenas para analise necessaria, com contexto limitado e custo contabilizado.
Qualifica o achado, atualiza ticket existente ou cadastra demanda unica na fila atual.
A esteira corrige, valida, revisa, integra e verifica o resultado, registrando cobertura e custo.

## Fora de escopo
- Nao criar outro agendador, fila, catalogo de projetos, roteador ou portal concorrente.
- Nao reler todo repositorio com LLM em cada rodada nem gerar tickets sem evidencia ou repetidos.
- Nao elevar tetos, flexibilizar piso de cota, ignorar hosts desabilitados ou alterar governanca protegida.
- Nao executar teste completo concorrente no mesmo host nem substituir o portao oficial.
- Nao declarar projeto saudavel quando revisao falhou, e parcial ou nao aplicavel.

## Criterios de aceite
- Conferir os consumidores reais do coordenador, fila, roteador e fontes existentes; adaptar o minimo e fatiar implementacao em unidades pequenas sem duplicar capacidades.
- Demonstrar em projeto darkfac uma rotina realmente acionada pelo coordenador existente, sem pedido manual, com estado persistido e evidencia de execucao.
- Demonstrar com portao comportamental que repeticao/concorrencia/retomada do mesmo achado nao duplica ticket ou execucao, e que queda de host preserva trabalho devido sem avalanche de execucoes atrasadas.
- Demonstrar reaproveitamento de evidencia valida sem nova inferencia e invalidacao quando codigo, dependencias, regras relevantes ou advisory externo mudam, mesmo sem novo commit.
- Os tres dominios iniciais produzem achados com referencia verificavel: procedimento documental confrontado com comportamento, vulnerabilidade aplicavel a dependencia identificada e desvio observado em custo/tempo/retries; evidencias ausentes nunca viram resultado inventado.
- Cada execucao diferencia sucesso sem achados, parcial, falha, espera por recurso e nao aplicavel, com cobertura efetiva, custo observado e motivo de adiamento.
- Demonstrar um achado real atravessando a fila unica ate correcao entregue e rechecagem observavel, respeitando orcamento, revisao cruzada, CI, deploy e smoke.
- Desativar o novo dominio por projeto impede novas admissoes sem apagar evidencias/tickets nem afetar o motor regular; consolidar testes existentes e atualizar manual, fase atual e learning pack.
- Corrigir e demonstrar a consistencia entre fila persistente, consulta de demanda e projecao do Hub: ticket real cadastrado pode ser lido na API e observado no consumidor apropriado sem esperar snapshot de arquivo; ID realmente ausente continua HTTP 404 e falha de store nao vira ausencia ou sucesso em memoria.
