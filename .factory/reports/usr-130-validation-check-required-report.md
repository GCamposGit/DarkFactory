# Relatório de Implementação — USR-130

## Identificação
- **Ticket**: `USR-130`
- **Título**: Validação sem checagem executada não pode autorizar integração
- **Branch**: `feature/usr-130-validation-executed-check-required`
- **Data**: 2026-10-06

## 1. Problema e Diagnóstico
Anteriormente, rotinas de checagem podiam considerar ausência de comandos de teste ou comandos vazios como "verdes" tacitamente (por exemplo, loops iterando sobre listas vazias sem falhas). No estágio de validação limpa (`ValidationStage`) e na validação pós-resolução de conflito em integração (`IntegrationStageHandler._run_validate`), a ausência de comandos de teste executados não pode autorizar a transição para etapas posteriores nem conceder aprovação/integração.

## 2. Mudanças Implementadas
1. **Garantia de Execução no `ValidationStage` (`core/line/stage_build.py`)**:
   - `executable_validate` filtra rigorosamente comandos não vazios (`[cmd for cmd in commands.validate_cmds if cmd.strip()]`).
   - Se nenhum comando for executável, o estágio registra `"ran": False, "ok": False` em `validation.json` e retorna `retry` para desenvolvimento com causa estruturada `clean_validate_missing:{sha[:12]}`.
   - Adicionada verificação explícita pós-execução garantindo que `commands["validate"]` efetivamente executou (`ran == True`) e passou (`ok == True`) para que `all_ok` seja verdadeiro.
   - A evidência gravada em `validation.json` vincula obrigatoriamente o `sha` verificado à checagem realizada.
2. **Garantia de Execução em Integração (`core/line/stage_integration.py`)**:
   - `_run_validate` filtra `executable_validate` e rejeita categoricamente listas vazias ou comandos contendo apenas espaços em branco, retornando `False, "Nenhum comando de validate foi detectado apos resolver o conflito."`.
   - Se o comando de validação falhar, o merge é impedido e o estágio falha com `merge_conflict_validate_failed`.
3. **Controle Positivo e Negativo**:
   - Controle negativo: comandos vazios (`[]`) ou em branco (`["  "]`) nunca retornam sucesso.
   - Controle positivo: comandos executáveis válidos com saída limpa retornam sucesso e vinculam a evidência ao SHA da árvore.

## 3. Cobertura de Testes
Validados testes determinísticos em `tests/line/test_stage_build.py` e `tests/line/test_stage_integration.py`:
- `test_clean_validation_without_validate_command_returns_to_development`:
  - Parameterizado com `[]` e `["  "]`.
  - Comprova retorno para `development` com causa `clean_validate_missing` e registro de `"ran": False, "ok": False`.
- `test_clean_validation_success_when_everything_is_committed`:
  - Comprova retorno `success` e existência de `validation.json` contendo `sha` válido e `"ran": True, "ok": True`.
- `test_conflict_resolution_cannot_pass_without_validate_command`:
  - Parameterizado com `[]` e `["  "]` no `IntegrationStageHandler`.
  - Comprova que `_run_validate` retorna `False` e log explicativo.
- `test_conflict_resolution_passes_with_executable_validate_command`:
  - Controle positivo de integração: executa comando válido e comprova retorno `True`.
- `test_conflict_resolution_fails_when_validate_command_fails`:
  - Controle de falha: executa comando que retorna código 1 e comprova retorno `False`.

## 4. Critérios de Aceite
- [x] **Sem comando/check executado, estágio não retorna success**: Garantido tanto em `ValidationStage` quanto em `IntegrationStageHandler._run_validate`.
- [x] **Evidência inclui pelo menos uma checagem vinculada ao SHA**: `validation.json` armazena `sha` e a execução da checagem com timestamp e status.
- [x] **Testes cobrem ausência de comandos e controle positivo**: Coberto em testes unitários e de integração parametrizados.
