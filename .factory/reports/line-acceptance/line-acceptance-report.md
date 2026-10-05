# Relatorio de Aceite da Linha Autonoma (HF-27 V1-V4)

**Data de Geracao**: 2026-10-05T20:00:20.479993+00:00  
**Status Geral**: VALIDANDO (EM PROGRESSO)  
**Criterios Aprovados**: 2/4  

---

## Criterios de Aceite

### V1: Demanda autonoma ponta a ponta sem intervencao humana alem do intake/grill
- **Veredito**: [PENDING]
- **Evidencia**: Nenhum run entregue de ponta a ponta verificado no control store

### V2: Canario diario E2E com streak >= 7 dias verdes
- **Veredito**: [PENDING]
- **Evidencia**: Streak atual: 0/7 dias verdes (ultimo: nenhum)
- **Detalhes**: `{"streak": 0, "min_streak": 7, "last_date": null}`

### V3: Failover de capacidade, graceful degradation e restricoes de quota
- **Veredito**: [PASS]
- **Evidencia**: Roteamento por capacidade, failover de quota e expiracao de espera verificados
- **Detalhes**: `{"dev_harness": "claude", "cooldown_failover_ok": true}`

### V4: Adocao greenfield e decomposicao de marcos (core.adoption)
- **Veredito**: [PASS]
- **Evidencia**: Servico core.adoption (plan_adoption, apply_adoption, verify_adoption) e ProjectKind.GREENFIELD verificados
- **Detalhes**: `{"service": "core.adoption.service", "types": ["greenfield", "brownfield"]}`

---

**Resumo**: 2/4 criterios de aceite verificados (V1: False, V2: False, V3: True, V4: True).
