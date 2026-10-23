# DEMAND

- projeto: darkfac
- canal: line

## Texto original

# Roteador sem rota com Grok a 17,4%% e probe Anthropic ok mas snapshot parado ha 32h

## Problema
Em 22/10 06:55 python run_ticket.py USR-346 saiu com 'Nenhuma rota disponivel'. Log: codex pulado (codex_headless_visible_windows); 'Snapshot for anthropic is stale (115537.9 s)... probe provider=anthropic outcome=ok' seguido de 'Quota data for anthropic is older than 3600.0s (probe_unavailable); failing closed' -- o probe teve sucesso mas o snapshot nao foi atualizado; grok com 17,4%% restante (acima do piso de 15%%) nem foi avaliado/registrado no log. Resultado: fila de desenvolvimento parada no Notebook com conta saudavel.

## Jornada
Em 22/10 06:55 python run_ticket.py USR-346 saiu com 'Nenhuma rota disponivel'. Log: codex pulado (codex_headless_visible_windows); 'Snapshot for anthropic is stale (115537.9 s)... probe provider=anthropic outcome=ok' seguido de 'Quota data for anthropic is older than 3600.0s (probe_unavailable); failing closed' -- o probe teve sucesso mas o snapshot nao foi atualizado; grok com 17,4%% restante (acima do piso de 15%%) nem foi avaliado/registrado no log. Resultado: fila de desenvolvimento parada no Notebook com conta saudavel.

## Fora de escopo
- Nao relaxar o piso de 15%%

## Criterios de aceite
- Probe Anthropic com outcome=ok grava snapshot novo e o roteador usa o valor medido
- Grok acima do piso e elegivel para development no Notebook, ou o log diz o motivo exato da exclusao
- Teste reproduz o caso do log de 22/10
- Portao oficial runner.py --quick passa
- Decisao do owner em 23/10/2026: o bloqueio do Codex headless no Windows (USR-336) era temporario; sem DARKFAC_CODEX_HEADLESS_ALLOWED no processo o Codex volta a ser elegivel no Notebook (o bloqueio so vale com opt-out explicito)
- pick('development', ['harness:claude','harness:codex','harness:grok','harness:antigravity']) no Notebook devolve um harness com write sempre que algum estiver acima de 15%; devolver None nesse caso e defeito
- Grok nunca e excluido em silencio: cada harness descartado gera log com o motivo (hoje pick com so harness:grok devolve None sem nenhuma linha de log)
