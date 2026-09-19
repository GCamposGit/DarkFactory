# Jev: probes de roteamento da Dark Factory (2026-09-19)

## Escopo

Foram feitas 11 chamadas reais ao Jev `typesafe/jev-1.13` pelo endpoint
OpenRouter Decisions, com tarefas sintéticas que imitam demandas da fábrica.
Nenhum conteúdo de cliente, credencial ou patch privado foi enviado. O modelo
efetivo retornado pela API foi `typesafe/jev-1.13-20260917`.

Oito chamadas pediram quatro julgamentos independentes sobre a mesma tarefa:
etapa imediata (`Choice`), complexidade (`Choice`), necessidade de decisão do
owner (`Noul`) e exigência explícita de operação local (`Noul`). Três chamadas
pediram um executor entre candidatos previamente filtrados, com política e
ordem do catálogo declaradas. Rótulos esperados foram definidos antes das
chamadas; complexidade e ambiguidade têm componente subjetivo.

| Caso | Etapa esperada → Jev | Complexidade esperada → Jev | P(decisão owner) | P(local) |
| --- | --- | --- | ---: | ---: |
| Correção UTF-8 localizada | coding → coding | low → low | 0,30 | 0,25 |
| Arquitetura de orçamento concorrente | planning → planning | high → high | 0,76 | 0,12 |
| Revisão independente | review → review | medium → medium | 0,48 | 0,13 |
| Execução de testes determinísticos | testing → testing | low → low | 0,35 | 0,14 |
| Correção confidencial offline | coding → coding | medium → medium | 0,46 | 0,96 |
| Pesquisa em docs atuais | research → research | medium → low | 0,32 | 0,11 |
| Pagamentos com regras ausentes | planning → planning | high → high | 0,96 | 0,08 |
| Incidente crítico de autenticação | planning → planning | critical → critical | 0,70 | 0,13 |

Resultado observado: **8/8 etapas** e **7/8 complexidades** coincidiram com
os rótulos prévios. A complexidade da pesquisa em docs foi o desacordo. A
distribuição da revisão ficou quase dividida entre `medium` (0,42) e `high`
(0,40); a correção confidencial também teve distribuição espalhada. O `Noul`
para decisão do owner marcou 0,76 na tarefa de arquitetura com regras já
definidas, um falso positivo plausível se fosse usado como gate.

Nas três chamadas de escolha de executor, Jev selecionou o primeiro candidato
elegível de acordo com a política explícita: Qwen local rápido, DeepSeek como
fallback de alta arquitetura após quota do primário e GPT Review para revisão
independente. As três respostas atribuíram probabilidade 1,0 ao escolhido.
Esses casos eram resolvíveis por código a partir do catálogo; o acerto não
mostra que Jev prevê qualidade futura de modelos.

As 11 chamadas consumiram **8.173 tokens de entrada**, custaram **US$ 0,00034327**
segundo as respostas da API e tiveram latência mediana de **419,8 ms** neste
ambiente (mínimo 316,8 ms; máximo 609,7 ms). Esses números descrevem somente
estas chamadas, não um benchmark representativo de desempenho ou calibração.

## Interpretação para integração

1. Melhor encaixe inicial: extrair etapa e sinais de complexidade da demanda
   antes de consultar o catálogo. Registrar distribuição completa, versão do
   modelo, input sanitizado e resultado posterior do job.
2. Decisão de owner: usar como sinal para revisão do Grill, jamais como
   declaração automática de `ready_for_spec` ou `WAITING_HUMAN`. Um caso sem
   lacuna material clara recebeu probabilidade 0,76.
3. Privacidade: requisitos explícitos de offline devem ser extraídos como fatos
   do contrato e aplicados por política determinística. A classificação Jev pode
   ajudar a encontrar indícios, mas não autorizar tráfego de dados.
4. Escolha final do executor: manter qualificação, cotas, orçamento, isolamento
   de famílias e catálogo no `select_route`. Não gastar uma chamada Jev para
   escolher o primeiro candidato elegível. Para seleção estatística por tarefa,
   é preciso ampliar resultados reais por classe e medir benefício incremental.

Arquivos: `simulate.py` e `candidate_probes.py` reproduzem as chamadas;
`results.json` e `candidate_results.json` contêm inputs sintéticos e respostas
completas, sem segredos.
