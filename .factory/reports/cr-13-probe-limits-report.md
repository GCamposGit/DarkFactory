# Relatório de Conclusão — CR-13: Cumprir Limites dos Probes HF-01

**Data:** 11/09/2026  
**Status:** Concluído com Sucesso (100% PASS)  
**Ticket:** CR-13 (Trilha HF-01 Confiança, Complementos Locais)  
**Achado endereçado:** F14 (probes HTTP sem limite estrito de timeout de 3s por probe e payload máximo de 65.536 bytes com risco de vazamento de corpo sensível em falhas)  
**Documentos canônicos:** `docs/handoffs/HF-REVIEW-REMEDIATION-2026-09-09.md`, `docs/reviews/HF_CRITICAL_REVIEW_2026-09-09.md`, `core/planning/baseline_probes.py`, `tests/test_baseline_probes.py`

---

## 1. Escopo e Invariantes Implementadas

1. **Constantes e Contratos Limítrofes Estritos**:
   - `MAX_PROBE_BYTES: int = 65_536` (64 KiB) e `MAX_PROBE_TIMEOUT_SECONDS: float = 3.0` (3 segundos) definidos em `core/planning/baseline_probes.py` e exportados em `__all__`.
   - `ProbeSpec` impõe via validação de campos Pydantic v2:
     - `timeout_seconds: float = Field(default=MAX_PROBE_TIMEOUT_SECONDS, gt=0, le=MAX_PROBE_TIMEOUT_SECONDS)`
     - `max_bytes: int = Field(default=MAX_PROBE_BYTES, gt=0, le=MAX_PROBE_BYTES)`
   - Qualquer tentativa de instanciar `ProbeSpec` com `timeout_seconds > 3.0` ou `max_bytes > 65536` (ou `<= 0`) falha imediatamente com `pydantic.ValidationError`.

2. **Fronteira Exata de Payload (Anti-Leak de Dados Sensíveis)**:
   - Respostas de até exatamente 65.536 bytes são aceitas com `ProbeStatus.OK`, calculando `response_size=65536` e `response_sha256`.
   - Respostas de 65.537 bytes ou superiores são rejeitadas como `ProbeStatus.RESPONSE_TOO_LARGE` com `error_code="PROBE_RESPONSE_TOO_LARGE"`.
   - Nesses casos, `response_size` e `response_sha256` permanecem `None`, e o corpo da resposta nunca é retido na observação, eliminando integralmente riscos de vazamento de credenciais, tokens de autenticação ou payloads confidenciais em logs ou JSON dumps.

3. **Controle Estrito de Deadline com Relógio Injetável**:
   - `probe_endpoint` e `collect_probe_observations` aceitam parâmetro opcional `timer: Callable[[], float] | None = None` (com fallback para `time.monotonic`).
   - Respostas que demoram até exatamente 3.0s são aceitas como `ProbeStatus.OK`.
   - Respostas que demoram mais de 3.0s (ex.: 3.001s) ou falham com erros HTTP tardios além do deadline são categorizadas deterministicamente como `ProbeStatus.TIMEOUT` com `error_code="TIMEOUT"`.

4. **Tratamento Estruturado de Falhas de Transporte**:
   - Falhas de rede (`ConnectionRefusedError`, `urllib.error.URLError`, `OSError`) produzem observações estruturadas com `ProbeStatus.NETWORK_ERROR` e `error_code="PROBE_TRANSPORT_ERROR"`.
   - Nenhum endpoint confidencial, porta interna ou mensagem bruta com segredos vaza na serialização da observação.

---

## 2. Arquivos Modificados

| Arquivo | Mudança |
|---|---|
| `core/planning/baseline_probes.py` | Definidas constantes `MAX_PROBE_BYTES = 65_536` e `MAX_PROBE_TIMEOUT_SECONDS = 3.0`. Atualizados limites de campos no `ProbeSpec`. Adicionado suporte a timer injetável e verificação de prazo decorrido em `probe_endpoint` e `collect_probe_observations`. Exportadas novas constantes em `__all__`. |
| `tests/test_baseline_probes.py` | Adicionados 6 casos de teste abrangentes cobrindo rejeição de `timeout_seconds > 3.0`, rejeição de `max_bytes > 65536`, fronteira exata de 65.536 bytes aceitos, rejeição de 65.537 bytes sem leak de dados sensíveis, controle de relógio com prazo exato vs. excedido e falhas de transporte estruturadas. |

---

## 3. Validação Executada

### 3.1 Testes Unitários Focais
```powershell
python -m pytest tests/test_baseline_probes.py -v
```
Resultado: **15 passed in 0.10s**

### 3.2 Harness Determinístico Completo
```powershell
python core/harness/runner.py --quick
```
Resultado:
- `[STEP_PASS] syntax_and_types`
- `[STEP_PASS] unit_and_integration_tests`
- **614 passed, 2 skipped in 72.32s**
- `[HARNESS_PASS]`

### 3.3 Suíte Global de Testes do Repositório
```powershell
python -m pytest tests -v
```
Resultado: **614 passed, 2 skipped in 64.68s**

---

## 4. Conclusão

O ticket **CR-13** foi remediado em conformidade estrita com o finding F14 do `HF-REVIEW-REMEDIATION-2026-09-09.md`. A Trilha HF-01 (Confiança de Baselines e Probes) avança sem regressões e com 100% de conformidade com os portões determinísticos da Dark Factory.
