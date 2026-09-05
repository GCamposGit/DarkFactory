---
name: validation-harness
description: Constrói e opera o harness determinístico de validação com escada de 5 níveis (estático, unitário, integração, E2E headless e testes holdout). Emite marcadores rígidos invioláveis por prompts de LLM. Use ao configurar a validação de um projeto ou ao diagnosticar falhas de suíte de testes.
---

# Validation Harness: A Escada Determinística de Testes

O *Validation Harness* é o componente mais crítico de uma fábrica autônoma de software. Enquanto o orquestrador decide o que rodar, o harness decide se o que rodou tem valor e pode ser enviado para produção.

> **Regra de Ouro**: O portão de aprovação é código executável, nunca uma resposta em linguagem natural do modelo. O modelo não pode "convencer" o harness de que o código funciona.

## A Escada de Validação (The 5-Level Ladder)

1. **Nível 1: Análise Estática & Tipos**
   - Linting, verificação de sintaxe e compiladores de tipos (`pyright`, `tsc`, `cargo check`).
   - Rápido (poucos segundos) e obrigatório em todo `--quick`.
2. **Nível 2: Testes Unitários**
   - Testa funções isoladas e casos de borda com mocks controlados.
3. **Nível 3: Testes de Integração**
   - Valida a integração entre módulos, persistência e contratos de interface.
4. **Nível 4: E2E Headless (Como o Usuário Real)**
   - O teste simula a jornada real do usuário usando um dos três drivers:
     - `http`: sobe o servidor em porta dinâmica, dispara requisições HTTP e valida respostas.
     - `cli`: executa o binário com argumentos reais e valida saídas.
     - `library`: importa a biblioteca e chama os métodos públicos.
5. **Nível 5: Testes Holdout (Isolamento de Fraude)**
   - Testes armazenados em diretório protegido (`.factory/holdout/`) que o modelo não pode ver nem modificar. Garante que o modelo não sobreajustou o código apenas para enganar os testes visíveis.

## Contrato de Marcadores Estruturados

O runner (`core/harness/runner.py`) emite marcadores padronizados:
- `[STEP_START] <nome_do_passo>`
- `[STEP_PASS] <nome_do_passo>`
- `[STEP_FAIL] <nome_do_passo>`
- `[TEST_COUNT] count=<N>`
- `[HARNESS_PASS]` / `[HARNESS_FAIL]`

O script `core/harness/markers.py` valida o stream:
- **Empty is not pass**: Se zero testes rodaram, o veredito é FAIL.
- **Zero discovered is a failure**: Se o discovery do framework de testes encontrar 0 testes, o veredito é FAIL.

---

## 🧠 Continuous Self-Improvement & Failure RCA Integration

1. **Síntese de Testes de Regressão a partir de Erros (Never Repeat a Bug)**:
   - Toda vez que um bug ou falha for identificado (seja em tempo de desenvolvimento ou apontado pelo usuário), é obrigatório escrever um teste unitário ou de integração que reproduza a falha e passe apenas com a correção.
   - O teste criado passa a integrar a suíte oficial permanente no `harness.config.json`, impedindo qualquer regressão futura.
2. **Auditoria de Causa Raiz Determinística**:
   - Falhas no Nível 1 (tipos/sintaxe) são registradas no RCA para adicionar type annotations mais estritas no código.
   - Falhas no Nível 4 (E2E) geram asserções adicionais de contrato e validação de payload HTTP/CLI.
3. **Extrapolação de Cobertura**:
   - Se um caso de borda falhou em uma entidade (ex.: chave ausente no JSON), adicione testes para chaves ausentes em todas as entidades e schemas correlatos.

