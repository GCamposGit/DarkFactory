# DEMAND

- projeto: darkfac
- canal: line

## Texto original

# Qualificacao real de modelos em producao: executor com teto de gasto e preenchimento de precos

## Problema
Residuo do USR-185 (22/10/2026): autopilot.run_daily so tem o gancho qualify_func, sem executor real de modelos; candidatos descobertos ficam sem qualificacao em producao e, sem preco conhecido, sempre caem no Grill ('preco desconhecido'). Depende da decisao do owner sobre teto de gasto e chaves (OA de decisao).

## Jornada
Residuo do USR-185 (22/10/2026): autopilot.run_daily so tem o gancho qualify_func, sem executor real de modelos; candidatos descobertos ficam sem qualificacao em producao e, sem preco conhecido, sempre caem no Grill ('preco desconhecido'). Depende da decisao do owner sobre teto de gasto e chaves (OA de decisao).

## Fora de escopo
- Nao gastar acima do teto aprovado nem relaxar o piso de cota de 15%.

## Criterios de aceite
- Executor de qualificacao real chama o modelo candidato pela API do provedor com teto de gasto por candidato e por dia lido do registro; acima do teto, recusa sem chamar.
- Precos do candidato preenchidos a partir do OpenRouter/benchmark (skill 12) quando disponiveis; ausentes continuam indo ao Grill.
- Testes com HTTP falso; nenhum gasto sem a decisao do owner registrada.
