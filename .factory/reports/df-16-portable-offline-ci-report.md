# DF-16 — Portable offline audio tests and CI

## Resultado

DF-16 foi implementado sem alterar o experimento Canaletto e sem adicionar o stack opcional de GPU à suíte oficial.

- Testes de áudio usam um WAV estéreo sintético e um modelo Whisper falso, executando em CPU e sem rede.
- O ensaio real `live`/`gpu` foi separado, marcado e bloqueado por padrão.
- A fixture global remove credenciais conhecidas, ativa os modos offline de hubs de modelos e bloqueia URL/TCP externo; loopback e sockets locais necessários ao `TestClient` permanecem funcionais.
- A CI preserva `trusted-pr-policy` e `pr-validation`, adiciona matriz Linux/Windows aos jobs de validação e executa os dois comandos oficiais.
- O ambiente de CI força UTF-8 e a materialização do verificador usa Bash também no runner Windows.

## Arquivos

- `tests/test_audio_transcriber.py`
- `tests/conftest.py`
- `pytest.ini`
- `.github/workflows/ci.yml`
- `.factory/reports/df-16-portable-offline-ci-report.md`

## Validação final

Executada em Windows com Python 3.12, `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8` e o runtime no `PATH`:

- `python -m pytest tests -v --ignore=tests/test_canaletto.py`: **188 passed, 1 skipped**, 2 warnings upstream.
- `python core/harness/runner.py --quick`: **[HARNESS_PASS]**, 189 descobertos, 188 passados e 1 skip.
- `python -m pytest tests/test_audio_transcriber.py -v`: **4 passed, 1 skipped**.
- `python -m pytest tests/test_anti_slop_engine.py tests/test_hub.py -v`: **25 passed**.
- `python -m compileall -q core hub run_hub.py tests/test_audio_transcriber.py tests/conftest.py`: passou.

O skip é exclusivamente `test_transcribe_stereo_meeting_file_live_gpu`, por falta de opt-in. Para executar manualmente, é necessário fornecer as dependências/artefatos apropriados e usar `--run-live-audio --run-gpu-audio`; `--allow-network` é necessário se o ensaio precisar baixar modelos.

## RCA registrado

- `rca_a742a0c9`: bloqueio de sockets amplo demais interferia nos transportes locais do `TestClient`; o guard foi restrito a destinos externos.
- `rca_7cbd0804`: `socket.AF_UNIX` não existe em Windows; a checagem agora é defensiva com `getattr`.
- `rca_19a261b9`: o runtime local não estava no `PATH`, embora a CI o disponibilize via `setup-python`; a validação local foi repetida com o ambiente correto.

Nenhum commit foi criado.
