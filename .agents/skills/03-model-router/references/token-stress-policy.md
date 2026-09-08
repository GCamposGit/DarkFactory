# Política de pressão de tokens

Use `core/router/token_budget.py` antes de despachar tarefas quando houver telemetria de cota. O previsor é determinístico e barato: combina baseline por tipo de tarefa, `chars/4`, complexidade e quantidade limitada de etapas.

## Ordem de decisão

1. Avalie todas as contas configuradas; não presuma que a conta primária representa a capacidade total.
2. Se a conta preferida estiver baixa, use outra conta com maior headroom horário.
3. Preserve uma reserva de 15%: quando as assinaturas atingirem essa faixa e houver gateway pago configurado, use a API paga antes de esgotá-las.
4. Sob pressão, priorize testes, scripts, linters e modelos Ollama. Adie, quando possível, arquitetura, PRDs e pesquisa profunda.
5. Troque velocidade e qualidade marginal por continuidade: menor esforço de raciocínio, teto de saída menor e blocos modulares curtos.

## Faixas

| Restante na janela curta | Estado | Política |
| --- | --- | --- |
| acima de 50% | `healthy` | rota normal |
| 25–50% | `guarded` | saída reduzida, entrega modular |
| 10–25% | `stressed` | baixo esforço; local/script quando compatível |
| até 10% | `critical` | esforço mínimo; preservar assinatura e priorizar execução local |

O CLI `recommend` consulta todas as contas por padrão. Use `--no-quota-scan` apenas para execução determinística sem probes; use `--remaining-hourly-percent` para simulação ou teste.

O resultado expõe `token_budget` com previsão, pressão, conta/provedor escolhido, teto de saída, tamanho do bloco e prioridade. Consumidores devem respeitar `max_output_tokens`, `block_output_tokens`, `reasoning_effort`, `task_priority` e `defer_frontier_work`.
