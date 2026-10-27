# Plano V0 Operacional (congelamento de 11/10/2026)

Decisão do owner em 11/10/2026: a própria DarkFac é a primeira entrega real e o owner é o cliente. Antes de abrir qualquer projeto novo, a linha precisa ser operacional e confiável. Tudo que não estiver no caminho crítico fica congelado até o V0 ser atingido.

## 1. O que é o V0

Decisão do owner em 16/10/2026: falhas intermediárias, retomadas e resgates fazem parte do desenvolvimento e não zeram o contador de entregas. Esta decisão substitui a exigência anterior de cinco lançamentos únicos sem intervenção. O V0 está atingido quando todas as condições abaixo valem ao mesmo tempo:

1. **5 entregas integradas.** Os cinco tickets `v0-critico` da sequência definida chegam à `main` e constam como `completed` no ledger. Falhas, novas tentativas e resgates não removem entregas já comprovadas nem reiniciam a contagem.
2. **`main` verde** em Linux e Windows ao fim da sequência.
3. **Deploy limpo.** `python C:\dev\DarkFac\scripts\dokploy_redeploy.py` sai com código 0 e backup verificado depois do lote.
4. **Falha explícita.** Toda falha durante a sequência termina num estado claro (ticket de volta a `planned` com o motivo, HumanRequest ou OA), nunca em silêncio nem em falso sucesso.

Cada defeito continua exigindo correção ou ticket formal. Uma dívida aberta só impede o encerramento quando bloqueia uma das condições acima; a existência de resgate, por si só, não impede o V0.

O V1 (demanda pelo Telegram vira deploy sozinha) e o V2 (canário verde por 7 dias) continuam no `PRODUCTION_LINE_PLAN_2026-09-22.md` e só voltam depois do V0.

## 2. Tickets críticos (tag `v0-critico`, horizonte `now`)

Plano histórico de 11/10/2026. Cada onda reduz os obstáculos à entrega; o estado atual dos tickets está no ledger `.factory/demands/demands.json`. A sequência efetiva de cinco entregas e suas evidências estão abaixo.

| Onda | Ticket | Por que trava a linha |
|---|---|---|
| 0. Main verde | USR-223 | CI vermelho no `main` @dc89843. A `main` voltou a ficar verde em 147b3cf; verificar e fechar com a evidência. |
| 1. Recuperação da entrega | USR-231 | Lançar a entrega de dentro da worktree deixa pasta órfã (WinError 32). |
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

### Evidência atualizada em 16/10/2026

**Contador de entregas: 5/5.** A sequência foi registrada no PR #354 e os cinco tickets constam como `completed` no ledger. Os commits abaixo pertencem à `main`; horários de 15/10/2026 em Brasília (UTC-3).

| Ticket | PR integrado | SHA na main | Hora |
|---|---|---|---|
| USR-266 | #356 | `968c70d` | 15:27 |
| USR-265 | #357 | `02f148f` | 16:24 |
| USR-179 | #358 | `90b9676` | 16:48 |
| USR-192 | #360 | `df1705f` | 19:59 |
| USR-259 | #361 | `8ae3de7` | 21:05 |

**Encerramento ainda pendente:** comprovar CI verde da `main` em Linux e Windows, deploy com código 0 e backup verificado no lote de encerramento. O contador completo, isoladamente, não declara o V0 concluído; a condição de falha explícita também permanece obrigatória.

**Dívida aberta — USR-268:** o fallback local após perder os workers remotos excedeu 3000 segundos. O ticket permanece `planned`, sem alegação de correção: faltam reprodução, diagnóstico e comprovação de duração abaixo de 1800 segundos. Sob o critério atualizado, ele não zera o contador e pode permanecer aberto se não impedir as condições de encerramento. Se bloquear a validação do lote atual, torna-se bloqueador desse encerramento.

## 3. Congelados (tag `congelado-v0`, horizonte `later`)

Lista histórica do congelamento; o ledger determina o estado atual. USR-179 e USR-192 foram promovidos ao caminho crítico e concluídos na sequência acima. Nada foi cancelado por este plano.

- **Hub e painéis:** USR-206 (e as fatias USR-232 a 236), 207, 210, 213, 215, 216, 217, 218, 219, 220
- **Telemetria e Esteira ao vivo:** USR-208, 209, 212
- **Canais Hub/Telegram:** USR-194, 195, 204
- **Autonomia contínua (V1/V2):** USR-211 (canário), USR-221 (cron na VPS)
- **Modelos e subagentes:** USR-177, 185
- **Portão (consolidados em USR-178):** USR-135, 176, 200
- **Spike de runtime:** USR-157
- **Trilha de segurança e qualidade:** USR-170, 171, 172, 173, 174, 226, 227, 228

## 4. Regras durante o congelamento

1. Só se implementam tickets com a tag `v0-critico`. Pedido novo do owner fora do V0 entra como `congelado-v0`, a menos que o owner diga explicitamente que quer furar o congelamento.
2. A regra "defeito encontrado = correção ou ticket" continua valendo, com triagem: se o defeito impede alguma condição do V0, vira `v0-critico`; se não impede, é registrado com `horizon: later` e a tag `congelado-v0`.
3. Uma sessão opera a linha por vez. O checkout compartilhado e o Desktop (um portão por vez) não aguentam sessões concorrentes.
4. Tickets com no máximo 3 critérios de aceite. Ticket maior é fatiado antes de rodar.
5. Deploy uma vez por lote, com os workers ociosos. O grokbox é o worker principal do portão; preservar a execução de uma suíte completa por vez.
6. Falhas, retomadas e resgates não zeram o contador. Encerrar o V0 somente com as cinco entregas comprovadas, CI Linux/Windows verde, deploy e backup verificados e falhas explícitas.

## 5. Descongelamento

**Prioridade absoluta depois do V0 (owner, 14/10/2026): entregar demanda de desenvolvimento de cliente de ponta a ponta, de qualquer tipo.** A fabrica e inutil se nao consegue. Ticket USR-259, já integrado no PR #361: run_ticket por projeto, no repositorio do cliente e com a validacao que o projeto declara, nunca a suite da fabrica. Tag `pos-v0-prioridade-absoluta`.

Quando o V0 for atingido, o owner revisa a lista congelada. Em seguida, ordem sugerida para o V1: USR-221 (cron na VPS), USR-207 (Hub fora do ar a cada merge), USR-211 e USR-215 (canário e painel de aceite). A trilha de segurança (USR-170 a 174 e 226 a 228) entra antes de qualquer projeto de cliente pago.
