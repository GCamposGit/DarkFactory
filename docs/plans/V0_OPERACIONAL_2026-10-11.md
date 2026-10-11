# Plano V0 Operacional (congelamento de 11/10/2026)

Decisão do owner em 11/10/2026: a própria DarkFac é a primeira entrega real e o owner é o cliente. Antes de abrir qualquer projeto novo, a linha precisa ser operacional e confiável. Tudo que não estiver no caminho crítico fica congelado até o V0 ser atingido.

## 1. O que é o V0

A linha entrega os próprios tickets sem resgate manual. O V0 está atingido quando todas as condições abaixo valem ao mesmo tempo:

1. **5 entregas seguidas sem resgate.** Cinco tickets `v0-critico` consecutivos lançados uma única vez com `python C:\dev\DarkFac\run_ticket.py <ID>` terminam em merge na `main`, sem nenhuma intervenção depois do lançamento: sem `--resume-delivery`, sem cirurgia de worktree, sem `gh run rerun`, sem patch pelo chat.
2. **`main` verde** em Linux e Windows ao fim da sequência.
3. **Deploy limpo.** `python C:\dev\DarkFac\scripts\dokploy_redeploy.py` sai com código 0 e backup verificado depois do lote.
4. **Falha explícita.** Toda falha durante a sequência termina num estado claro (ticket de volta a `planned` com o motivo, HumanRequest ou OA), nunca em silêncio nem em falso sucesso.

Uma entrega que precise de resgate zera o contador. A causa do resgate vira ticket `v0-critico`.

O V1 (demanda pelo Telegram vira deploy sozinha) e o V2 (canário verde por 7 dias) continuam no `PRODUCTION_LINE_PLAN_2026-09-22.md` e só voltam depois do V0.

## 2. Tickets críticos (tag `v0-critico`, horizonte `now`)

Ordem de execução. Cada onda reduz o resgate manual da onda seguinte.

| Onda | Ticket | Por que trava a linha |
|---|---|---|
| 0. Main verde | USR-223 | CI vermelho no `main` @dc89843. A `main` voltou a ficar verde em 147b3cf; verificar e fechar com a evidência. |
| 1. Entrega sem resgate | USR-231 | Lançar a entrega de dentro da worktree deixa pasta órfã (WinError 32). |
| | USR-225 | Uma nova tentativa depois de timeout cria worktree nova e o agente recomeça do zero. |
| | USR-224 | A limpeza da lixeira falha com acesso negado a cada entrega. |
| | USR-238 | PRs de registro de fila ficam abertos ou em conflito e o ticket nunca chega à `main` (USR-179, 192 e 222 ficaram presos de 08 a 11/10). |
| 2. Capacidade | USR-214 | Roteador bloqueia desenvolvimento por snapshot de cota velho. |
| | USR-201 | Modelo e esforço dos harnesses headless não são fixados nem registrados. |
| | USR-188 | Codex não abre terminal no `run_ticket` headless. |
| 3. Portão | USR-178 | Portão estoura o tempo sob concorrência e o worker do Desktop silencia (consolida USR-135, 176 e 200). Caminho protegido: patch em `docs/proposals` + OA. |
| | USR-183 | Watchdog do worker mata só o processo de topo. Caminho protegido. |
| 4. Deploy | USR-186 | Restart do worker após update deixa o worker desligado (bind 8080) e o deploy sai com código 1. |
| | USR-196 | Erro real do update do Desktop invisível; pull aborta e o backup é pulado. |
| | USR-222 | Deploy diverge e pula o backup quando o checkout do Notebook está em outra branch. |

Prova do V0: os tickets das ondas 2 a 4 são a sequência de 5 entregas. A linha prova que funciona entregando o próprio caminho crítico.

**Estimativa realista:** 13 tickets, ciclo de 1 a 1,5 h cada (agente, portão de ~27 min e CI de ~15 min), em série e com a taxa de falha atual: **7 a 10 dias úteis**. Os riscos são USR-178 e USR-183, que dependem de commit do owner em `core/harness/*`.

## 3. Congelados (tag `congelado-v0`, horizonte `later`)

Continuam registrados e com status `planned`. Nada foi cancelado.

- **Hub e painéis:** USR-206 (e as fatias USR-232 a 236), 207, 210, 213, 215, 216, 217, 218, 219, 220
- **Telemetria e Esteira ao vivo:** USR-208, 209, 212
- **Canais Hub/Telegram:** USR-192, 194, 195, 204
- **Autonomia contínua (V1/V2):** USR-211 (canário), USR-221 (cron na VPS)
- **Modelos e subagentes:** USR-177, 185
- **Portão (consolidados em USR-178):** USR-135, 176, 200; também USR-179 (velocidade do pytest focal)
- **Spike de runtime:** USR-157
- **Trilha de segurança e qualidade:** USR-170, 171, 172, 173, 174, 226, 227, 228

## 4. Regras durante o congelamento

1. Só se implementam tickets com a tag `v0-critico`. Pedido novo do owner fora do V0 entra como `congelado-v0`, a menos que o owner diga explicitamente que quer furar o congelamento.
2. A regra "defeito encontrado = correção ou ticket" continua valendo, com triagem: se o defeito impede alguma condição do V0, vira `v0-critico`; se não impede, é registrado com `horizon: later` e a tag `congelado-v0`.
3. Uma sessão opera a linha por vez. O checkout compartilhado e o Desktop (um portão por vez) não aguentam sessões concorrentes.
4. Tickets com no máximo 3 critérios de aceite. Ticket maior é fatiado antes de rodar.
5. Deploy uma vez por lote, com o Desktop ocioso.

## 5. Descongelamento

Quando o V0 for atingido, o owner revisa a lista congelada. Ordem sugerida para o V1: USR-192 (intake pelo Telegram), USR-221 (cron na VPS), USR-207 (Hub fora do ar a cada merge), USR-211 e USR-215 (canário e painel de aceite). A trilha de segurança (USR-170 a 174 e 226 a 228) entra antes de qualquer projeto de cliente pago.
