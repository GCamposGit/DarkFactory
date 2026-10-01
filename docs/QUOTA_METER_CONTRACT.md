# Contrato do medidor de cotas

O roteador usa a menor porcentagem **restante** entre as janelas conhecidas. Quota
desconhecida, leitura suspeita ou snapshot vencido bloqueia a rota; `limited` vale
0%. A idade vem de `checked_at`, não da hora de modificação do arquivo. O piso
operacional permanece 15%.

| Provedor | Fonte e campo bruto | Semântica | Janelas e reset | Conferência manual |
| --- | --- | --- | --- | --- |
| xAI/Grok | Grok Bot `GetSandUsageStatus` em `api2.cursor.sh`, `usagePercent` | **usado** em pontos percentuais; restante = `100 - usagePercent` | Pool semanal; `currentPeriodStart` e `nextResetTimestampUtc`. `hasAvailableUsage=false` limita a conta. Não há medição numérica independente da janela 5h. | [Grok usage](https://grok.com/?_s=usage): abrir a conta SuperGrok, conferir uso disponível e data de renovação. Endpoint é interno e pode mudar. |
| OpenAI/Codex | `codex app-server` `account/rateLimits/read`, `rateLimitsByLimitId.*.{primary,secondary}.usedPercent`; alternativamente `token_count.rate_limits` da sessão local | **usado** em pontos percentuais | Duração em `windowDurationMins`, normalmente 300 min e 10080 min; `resetsAt` por janela. | [Codex usage](https://chatgpt.com/codex/settings/usage): comparar os limites de 5h e semanais da mesma conta. |
| Anthropic/Claude | `api.anthropic.com/v1/messages`, headers `anthropic-ratelimit-unified-5h-utilization` e `anthropic-ratelimit-unified-7d-utilization` | **usado**, fração 0..1; multiplicar por 100 | 5h e 7d; headers `*-reset` em epoch seconds ou RFC 3339. | [Claude usage](https://claude.ai/settings/usage): comparar as duas janelas separadamente. A janela de 5h pode ser menor que a semanal e governa a rota. |
| Google/Antigravity | Language Server local `RetrieveUserQuotaSummary` (ou `GetAvailableModels`), `remainingFraction` | **restante**, fração 0..1; multiplicar por 100 | `window`/`bucketId` identifica semanal ou 5h; `resetTime` por bucket. | Abrir Antigravity, painel de uso de modelos, selecionar a conta e o mesmo modelo Gemini; [Google One](https://one.google.com/) mostra o plano, não necessariamente o bucket do editor. |

## Evidência e limitações

`tests/fixtures/usage/xai_2026-10-01.json` transcreve apenas os campos não
sensíveis do payload real fornecido pelo owner em 2026-10-01. O valor 1.40324
significa 1.4% usado e 98.6% restante. Não havia payloads reais de OpenAI,
Anthropic ou Google nesta worktree; nenhum fixture foi inventado para eles.
Para completar a validação independente, capturar os campos acima no host do
owner, remover identificadores e credenciais, registrar a data e comparar cada
janela com o painel oficial. Uma chamada trivial ao Grok pode não mover o
medidor devido à defasagem ou granularidade da cobrança.
Os headers `anthropic-ratelimit-unified-*`, assim como os endpoints locais de
Grok Bot e Antigravity, não têm contrato público estável; um formato novo deve
resultar em quota desconhecida até nova comparação com o painel.

Cada sonda real grava campos permitidos, interpretação e flags em
`.factory/usage/history/<provedor>.jsonl`. A rotação move o arquivo completo
para um arquivo com data após 30 dias; o arquivo ativo só recebe append. O
histórico é local e ignorado pelo Git. `scripts/quota_audit.py --expect xai=98`
compara a leitura com o painel e retorna erro se a diferença superar 5 pontos.
Leituras suspeitas falham fechadas e exigem conferência no painel.
Para confirmar uma leitura Grok suspeita após comparar o painel, executar
`scripts/quota_audit.py --provider xai --expect xai=VALOR --confirm`. O comando
só grava a confirmação local se a leitura bruta estiver a até 5 pontos do
valor informado; a confirmação vale apenas para o mesmo período e percentual.
