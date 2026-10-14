# Proposta: o pre-teste focal que expira para de descartar a saida parcial (USR-249)

`core/harness/*` e protegido por `core/orchestrator/guard.py`: o job `trusted-pr-policy` do CI reprova
qualquer PR que o altere, e so o owner aplica por commit direto. Por isso a fabrica NAO altera
`core/harness/test_subagent.py`; este documento e o patch ao lado
(`docs/proposals/USR-249-focal-timeout-partial-output.patch`) trazem a mudanca ja testada. O teste novo
vai como `.txt` (se estivesse em `tests/` na branch reprovaria o portao, porque so passa depois do
patch); o owner copia para `tests/` no mesmo commit.

| Arquivo | Papel |
|---|---|
| `core/harness/test_subagent.py` | `_decode_partial` helper + correcao do `except subprocess.TimeoutExpired` em `TestSubagentEngine.execute_local` |
| `docs/proposals/USR-249-test_focal_timeout_partial_output.py.txt` -> `tests/test_focal_timeout_partial_output.py` (novo, 3 testes) | regressao do bug e dos dois casos de controle |

## 1. Problema e evidencia

Parte do ticket USR-249 (`.factory/demands/demands.json`): em 12/10/2026, o incidente do USR-239 (gate
de 3000s + duas reinvocacoes de ~600s de pre-teste focal travado) deixou logs de 21 bytes
(`[TIMEOUT EXPIRED]`) sem nenhuma saida parcial -- nao havia como saber se o travamento foi lock de
suite, falta de memoria ou outra coisa, so pelo log.

Diagnostico no codigo atual de `core/harness/test_subagent.py`, `TestSubagentEngine.execute_local`:

```python
except subprocess.TimeoutExpired as exc:
    raw_stdout = exc.stdout or "" if isinstance(exc.stdout, str) else ""
    raw_stderr = exc.stderr or "" if isinstance(exc.stderr, str) else ""
    combined_output = raw_stdout + "\n[TIMEOUT EXPIRED]\n" + raw_stderr
    exit_code = 124
```

`subprocess.run(..., text=True, timeout=...)` passa `text=True`/`encoding="utf-8"`, mas quando o
subprocesso estoura o timeout o CPython levanta `TimeoutExpired` com os BYTES brutos ja bufferizados em
`.stdout`/`.stderr` -- a decodificacao para `str` normalmente acontece dentro de `Popen.communicate()`,
que nunca chega a rodar completa num timeout real (so em chamadas MOCADAS do `subprocess.run`, como nos
testes existentes que usam `unittest.mock.patch`, o valor chega direto como `str`, porque quem construiu
o mock escolheu). O guard `isinstance(exc.stdout, str)` tratava esse caso real como ausencia de dados e
jogava tudo fora, silenciosamente.

Prova: `docs/proposals/USR-249-test_focal_timeout_partial_output.py.txt` roda SEM o patch (controle
negativo) e o teste do caso de bytes reprova (`partial stdout must survive the timeout, not be
discarded`); os dois testes de controle (`str` e `None`) continuam passando, confirmando que o bug e
especifico do caminho de bytes, nao uma regressao geral do tratamento de timeout.

## 2. O que muda

- Novo helper `_decode_partial(value) -> str`: `str` passa direto, `bytes` decodifica com
  `errors="replace"` (nunca lanca em sequencia invalida), `None`/outro tipo vira `""`.
- `except subprocess.TimeoutExpired`: troca os dois `isinstance(..., str)` por `_decode_partial(...)`.
  Nenhuma outra linha do metodo muda; o contrato de `DistilledTestReport` (`exit_code=124`, log isolado
  em disco via `raw_log_path`) e o mesmo.
- Sem variavel de ambiente nova, sem mudanca de assinatura publica.

## 3. Passo exato do owner

Em `C:\dev\DarkFac`, no `main` atualizado (PowerShell, um comando por vez):

```powershell
git -C C:\dev\DarkFac pull --ff-only origin main
git -C C:\dev\DarkFac apply --check C:\dev\DarkFac\docs\proposals\USR-249-focal-timeout-partial-output.patch
git -C C:\dev\DarkFac apply C:\dev\DarkFac\docs\proposals\USR-249-focal-timeout-partial-output.patch
Copy-Item C:\dev\DarkFac\docs\proposals\USR-249-test_focal_timeout_partial_output.py.txt C:\dev\DarkFac\tests\test_focal_timeout_partial_output.py
python -m pytest C:\dev\DarkFac\tests\test_focal_timeout_partial_output.py C:\dev\DarkFac\tests\test_worker_onprem_test_subagent.py -q -p no:cacheprovider
git -C C:\dev\DarkFac add core/harness/test_subagent.py tests/test_focal_timeout_partial_output.py
git -C C:\dev\DarkFac commit -m "fix(harness): pre-teste focal preserva saida parcial em bytes quando o timeout estoura [USR-249]"
git -C C:\dev\DarkFac fetch origin
git -C C:\dev\DarkFac rebase origin/main
git -C C:\dev\DarkFac push origin HEAD:main
```

Notas:

- Commite SOMENTE os 2 caminhos acima (nunca `git add -A`: o checkout compartilhado pode ter alteracoes
  locais soltas de outras sessoes).
- Direto no `main`, sem PR (um PR seria reprovado pelo `trusted-pr-policy`). Antes do push,
  `python C:\dev\DarkFac\core\orchestrator\guard.py origin/main` acusa `GUARD VIOLATION` (esperado: o
  guard compara caminhos, nao autoria); depois do push o diff some e fica `[GUARD PASS]`.
- Nenhuma mudanca de produto vai junto; nao precisa de deploy (`scripts/dokploy_redeploy.py`).

## 4. Testes e verificacao

Verificado SEM worktree (limite de caminho do Windows numa worktree aninhada em `AppData\Local\Temp`):
o modulo patched foi carregado isolado via `importlib` sob o nome `core.harness.test_subagent` e os 3
testes de `USR-249-test_focal_timeout_partial_output.py.txt` foram executados contra ele.

- Com o patch: `test_timeout_with_bytes_partial_output_is_decoded_not_discarded`,
  `test_timeout_with_str_partial_output_still_works`,
  `test_timeout_with_none_partial_output_yields_empty_string` -- 3 passaram.
- Sem o patch (controle negativo, mesmo arquivo original): o teste de bytes reprova com
  `AssertionError: partial stdout must survive the timeout, not be discarded`; os dois de controle
  (`str`, `None`) continuam passando -- confirma que o bug e isolado ao caminho de bytes.
- `git apply --check` do patch contra `core/harness/test_subagent.py` em `144bf4d` (HEAD desta
  worktree): limpo.

O portao completo (`runner.py --quick`) e a suite completa de `tests/test_worker_onprem_test_subagent.py`
NAO foram rodados aqui (o owner roda no passo 3 acima); e esperado que passem sem alteracao, porque o
patch so troca o helper interno chamado no ramo `except subprocess.TimeoutExpired`, que nenhum teste
existente exercita com um `TimeoutExpired` real (os existentes mocam `execute_local` inteiro ou usam
`subprocess.run` real sem forcar timeout).

## 5. Riscos e limites

- `errors="replace"` pode trocar bytes invalidos por `U+FFFD` em vez de preservar o byte exato; aceitavel
  para log de diagnostico (o `raw_log_path` completo e so para leitura humana/triagem, nunca parseado
  byte a byte por `parse_output`, que so usa regex sobre texto).
- Nao resolve a causa raiz do travamento (falta de RAM, contencao no Notebook); so garante que, quando
  acontecer de novo, o log isolado em disco mostra o que a suite estava fazendo no momento do timeout.
