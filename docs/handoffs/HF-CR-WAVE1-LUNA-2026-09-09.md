# Liberação local da primeira onda — CR-09 e CR-01

Versão 1.0, 09/09/2026. Parent: [handoff de remediação](HF-REVIEW-REMEDIATION-2026-09-09.md).
Origem: instrução explícita do owner nesta tarefa: lançar Luna e revisar o resultado.
Planejador/revisor coordenador: `/root` (Codex/GPT-6). Implementador: `/root/luna_wave1`,
`gpt-5.6-luna`, esforço `xhigh`. Aprovação procedimental do plano: esta revisão do
coordenador, vinculada ao hash no manifesto local; não é receipt operacional de HF-04.

## Recorte e ownership

Uma frente de implementação, com CR-09 seguido de CR-01. Branch exclusiva criada:
`codex/hf-remediation-wave1-luna`. Cwd: `C:/dev/DarkFac`. HEAD de referência:
`0f57e9c0cb3cf1a5d02d8e934c162617ebe71db9`. Arquivos locais/untracked são inputs
preexistentes, preservados em backup com hashes; não constituem um commit desta onda.

Somente Luna escreve código nos cinco paths abaixo. O coordenador inspeciona,
registra o plano e produz artefatos de revisão, sem implementar em paralelo:

- `core/workflow/runtime.py`
- `tests/test_workflow_runtime.py`
- `spikes/runtime_choice/contracts.py`
- `tests/test_runtime_spike_contracts.py`
- `tests/test_runtime_spike_native.py`

Evidências/backup/manifesto: `.factory/test_logs/hf-wave1-luna/`. Relatório do
implementador: `.factory/reports/hf-wave1-luna-report.md`. Não alterar o restante
do produto, skills, projetos locais de demonstração ou artefatos históricos da auditoria.

Não há fan-out de escritores nem transferência para outro checkout nesta onda.
Trabalho preexistente permanece preservado. Não fazer commit dos arquivos inteiros
untracked como se fossem apenas o delta corretivo. A publicação depende de separar
e revisar a baseline antecedente; a avaliação local da onda não declara integração.

## Binding complementar ao plano

CR-09 usa erro de estado do domínio `ReadinessError` já existente, com código/mensagem
estável `LOCAL_RUNTIME_DELIVERY_FORBIDDEN` e sem valores sensíveis. Rejeitar registro,
transição e retorno de estado carregado `DELIVERED`, inclusive replay do mesmo estado,
antes de publicar esse estado ou de gravar efeito de avanço. Uma base legada contendo
esse terminal deve gerar rejeição explícita ao abrir/carregar o run, sem apagar,
reescrever ou rebaixar silenciosamente os dados. O teste usa cópia SQLite descartável.
Não criar `allow_delivery` nem antecipar CR-10. Caminhos locais válidos até revisão
independente, cancelamento e demais terminais locais mantêm o contrato vigente.

CR-01 preserva o driver e o protocolo públicos. Na implementação atual não há evento
espontâneo `ready`: a aceitação observável é `STARTED` após `START`. O teste de processo
deve gerar o arquivo via `LabConfig.model_dump_json()`, iniciar `python -m
spikes.runtime_choice.driver --config <arquivo>`, enviar START, manter stdin aberto
até COMPLETED, verificar status/etapas e efeito por consulta independente ao store,
então enviar SHUTDOWN e conferir exit 0. Deadline de 10 s e cleanup do processo/servidor
em todos os caminhos. Servidor somente loopback, porta efêmera, dados sintéticos.

Para `root_dir`, preservar os tipos de entrada públicos atuais (Path/str em Python
e string no JSON) e rejeitar tipos arbitrários com erro validável; não remover strict
globalmente. Confirmar a semântica de path existente/diretório e normalização, enum,
campo extra e booleano em controle numérico. Não ampliar a política de diretórios
para restringir todos os usuários ao workspace: somente as fixtures ficam sob temp.

## Execução e revisão

Preflight do terminal já passou com PowerShell sem perfil. Conferir branch, hashes
do manifesto e coleta focal antes da primeira escrita. Registrar red inicial e
resultado verde após cada unidade; comandos obrigatórios permanecem os do projeto:

```powershell
python core/harness/runner.py --quick
python -m pytest tests -v
```

Usar logs em disco e temporários exclusivos; não executar suítes concorrentes.
Os contraexemplos de outras unidades permanecem abertos e não justificam alterar
seus critérios nesta onda. O coordenador revisará o delta contra o backup e rodará
contraexemplos independentes focados nos mecanismos corrigidos.

Duas tentativas sem progresso ou lacuna de contrato retornam ao coordenador. Não
trocar o modelo solicitado, consumir reset ou usar API paga automaticamente.
O resultado local deve indicar precisamente o delta, testes e pendências de Git.
