# Revisão da primeira onda CR-09 / CR-01

Parecer: **`accepted_for_local_scope` para CR-09 e CR-01**. A revisão não encontrou
impedimento restante dentro do recorte implementado; as demais unidades de remediação
continuam abertas. Implementador solicitado: GPT-5.6 Luna, esforço `xhigh`,
subagente `/root/luna_wave1`. Coordenador/revisor: `/root` (Codex/GPT-6).
São passes separados da mesma família de modelos; não há alegação de diversidade
de famílias ou aprovação remota.

Plano vigente: [liberação local da onda](../handoffs/HF-CR-WAVE1-LUNA-2026-09-09.md).
Branch: `codex/hf-remediation-wave1-luna`. HEAD de base:
`0f57e9c0cb3cf1a5d02d8e934c162617ebe71db9`, mais os arquivos locais preservados
em `.factory/test_logs/hf-wave1-luna/before-files.zip`.

## Escopo

CR-09 contém o runtime local para que ele não registre, avance ou exponha um run
`delivered` sem a integração futura com verificador confiável. CR-01 corrige o
transporte da configuração Pydantic pelo CLI real, preservando as restrições de
tipo e compatibilidade de entrada previstas no plano.

O implementador possui cinco arquivos: runtime e seus testes; contratos do spike
e seus dois arquivos de testes. O revisor inspeciona o delta e mantém verificações
independentes fora da suíte do implementador. Correções de outras unidades, skills
e projetos locais de demonstração ficam fora desta onda.

## Procedimento de revisão

1. Comparar o conteúdo com o backup anterior ao despacho, incluindo arquivos
   untracked, e conferir ausência de alteração fora dos paths permitidos.
2. Ler os guardas de estado e os validadores, procurando avanço sem evento,
   rebaixamento silencioso de estado legado, coerção indevida e erro de transporte.
3. Executar os casos independentes pela API pública, com SQLite/servidor loopback
   exclusivos, stdin mantido até terminal e deadline de encerramento do processo.
4. Conferir saídas brutas dos comandos obrigatórios e registrar hashes finais.

Os casos independentes estão em
[test_wave1_independent.py](../../.factory/reviews/hf-wave1-luna/test_wave1_independent.py).
O teste positivo do CLI consulta o SQLite do runtime e o store de efeitos fora do
processo do driver. A revisão de CR-09 compara rows de runs/outbox antes/depois da
rejeição e verifica preservação de uma base legada sintética.

## Evidência e resultado

A interrupção inicial foi causada por limite de uso do subagente. Em 10/09 o mesmo
subagente foi retomado, sem trocar o modelo ou apagar o trabalho. Os resumos de red
e green de CR-09 recuperados são históricos; não substituem a captura bruta dos
comandos novos.

O harness novo de CR-09 passou com 503 testes coletados, 501 aprovados, 2 skips,
exit 0 e `[HARNESS_PASS]`; a fase de testes levou 151,92 s. Fonte:
`.factory/test_logs/hf-wave1-luna/cr09-harness-rerun.raw.log`.

O harness final da onda passou com 508 testes coletados, 506 aprovados, 2 skips,
exit 0 e `[HARNESS_PASS]` (122,43 s). A execução separada obrigatória de
`python -m pytest tests -v` também passou com
506 aprovados e 2 skips (121,25 s). Essa segunda execução é a validação conjunta
da onda; não é apresentada como execução isolada adicional de CR-09.

Os 15 casos independentes do coordenador passaram em 1,56 s, incluindo o CLI
real, a persistência observada externamente, a ausência de mutação após rejeição
de entrega e a preservação de dados legados. Os dois skips da suíte normal são
o ensaio live de áudio opt-in e criação de symlink indisponível neste Windows.

| Verificação | Resultado | Evidência bruta |
|---|---|---|
| Harness CR-09 | 501 passed, 2 skipped | `cr09-harness-rerun.raw.log` |
| Harness final | 506 passed, 2 skipped | `cr01-harness.raw.log` |
| Suíte completa separada | 506 passed, 2 skipped | `cr01-pytest-suite.raw.log` |
| Revisão independente | 15 passed | `coordinator-independent.raw.log` |
| Auditoria histórica reexecutada | 13 failed, 3 passed | `remaining-audit.raw.log` |

Os logs ficam em `.factory/test_logs/hf-wave1-luna/`. Na auditoria histórica,
os dois contraexemplos de entrega de CR-09 passaram; os 11 casos HF-04 e os dois
casos CR-10 continuam falhando. São defeitos fora desta onda, não regressões
introduzidas por ela. Não houve alteração dos testes históricos para esconder
esses resultados.

Durante a revisão, o coordenador devolveu dois pontos ao implementador:

- `TypeError` do validador de `root_dir` escapava do tratamento do CLI. Luna
  passou a gerar erro de validação; bool, objeto e null são rejeitados com
  `CONFIG_INVALID`, sem traceback nem conteúdo da entrada.
- O teste do subprocesso precisava ancorar cwd no repositório, encerrar a
  thread leitora antes de fechar pipes e evitar exigir STARTED na posição zero
  da lista. Luna incorporou esses ajustes antes dos gates finais.

O [manifesto do delta](../../.factory/reviews/hf-wave1-luna/reviewed-delta.json)
vincula os hashes anteriores/finais dos cinco arquivos. A conferência não
identificou mudança inesperada fora do escopo. O
[patch corretivo](../../.factory/reviews/hf-wave1-luna/wave1-delta.patch) separa
a onda da implementação herdada, e o
[registro de validação](../../.factory/reviews/hf-wave1-luna/validation.json)
preserva as contagens, comandos e limites do parecer.

O relatório do implementador está em
[hf-wave1-luna-report.md](../../.factory/reports/hf-wave1-luna-report.md).

## Limites de entrega

O HEAD não contém a implementação antecedente local de HF-02/HF-04/HF-05.
Versionar os cinco arquivos inteiros incluiria trabalho herdado além do delta
corretivo. Esta onda preserva o backup e produz patch por diferença; não houve
commit/push/PR/merge nem certificação de operação cloud/DBOS. A integração remota
depende de consolidar e revisar a baseline antecedente separadamente.
