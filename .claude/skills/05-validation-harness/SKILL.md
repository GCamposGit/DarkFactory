---
name: validation-harness
description: Constrói e opera validação determinística de sintaxe, tipos, unidade, integração, E2E headless e holdout; verifica oráculos, descoberta e vínculo das evidências ao candidato. Use para configurar ou diagnosticar o harness e avaliar a suficiência dos testes.
---

# Harness e qualidade da evidência

Verificar o comportamento exigido por uma entrada observável e um oráculo independente do resultado declarado pelo candidato. Um marker é saída de um processo; sua autoridade depende de quem executou/configurou o verificador e de qual candidato foi examinado.

## Escolher o nível que comprova o requisito

1. Estático: sintaxe, lint e tipos são checagens diferentes. `compileall` comprova compilação sintática; não afirmar que executou análise de tipos só porque o step se chama `syntax_and_types`.
2. Unidade: funções/contratos e casos de borda, com mocks rotulados.
3. Integração: interfaces entre módulos, banco, filesystem, concorrência e persistência quando exigidos.
4. E2E headless: biblioteca, CLI ou HTTP pelo caminho real do consumidor. Um driver CLI lê arquivo/configuração real em subprocesso; testar apenas sua função interna deixa esse caminho descoberto.
5. Holdout: casos independentes e controle real de acesso. Diretório chamado `.factory/holdout/` não prova invisibilidade ou proteção; verificar ownership/permissões e não declarar isolamento não demonstrado.

Em gates/evidências/processos, consultar [padrões de contratos verificáveis](../02-plan-product-architecture/references/contract-review-patterns.md). Cobrir ambos os erros: aceitar inválido e bloquear um fluxo válido. Testes que apenas atribuem `freshness=current`, `resolved` ou estado de revisão não demonstram produção/verificação desses fatos.

## Antes de executar

- Conferir SHA-base/candidato, arquivos untracked pertinentes, config confiável, driver e ambiente. Alterações não commitadas precisam de manifest por path/hash.
- Cada critério tem teste/oráculo correspondente e estágio em que pode ser satisfeito. Requisito novo de teste exige criação antes da execução; missing file não é red esperado.
- Use dados sintéticos e destinos/processos/volumes exclusivos para testes destrutivos. Prova local não substitui identidade/rota real do worker.
- Preserve suíte/config confiável. Reproduções de auditoria ainda não corrigidas podem ficar fora da suíte padrão, executadas explicitamente e registradas como vermelhas; isso não autoriza esconder o defeito.

## Execução e interpretação

Executar os comandos obrigatórios definidos pelo repositório. No DarkFac:

```powershell
python core/harness/runner.py --quick
python -m pytest tests -v
```

Verificar exit code, discovery positivo, passed/failed/skipped e required steps. Zero checks e zero discovery falham. Skip de prova obrigatória é pendência; skip opt-in já permitido mantém seu limite explícito. Corrigir ambiente de cache temporário sem trocar o oráculo quando uma restrição de filesystem só impede logs/cache.

Markers `[STEP_START]`, `[STEP_PASS]`, `[STEP_FAIL]`, `[TEST_COUNT]`, `[HARNESS_PASS]`/`[HARNESS_FAIL]` devem estar ligados ao resultado estruturado e candidato/config corretos. Não extrair autorização de uma substring de log escrita pelo implementador.

Quando houver falha, reproduzir pelo menor caminho que preserva o mecanismo, corrigir a causa ou devolver ao planejador se mudar contrato. Não substituir um teste difícil por outro mais fraco. Depois da correção, o teste de regressão significativo entra na suíte apropriada. Não replicar automaticamente o mesmo teste em todos os módulos sem confirmar o mesmo mecanismo.

## Entrega

Registrar comandos, ambiente, contagens, códigos, duração, hashes, artefatos e limites. Distinguir cobertura da base atual de funcionalidades futuras. Retestar após mudanças relevantes/falhas; não repetir suíte por rotina quando nenhum dado relevante mudou. Testes verdes de um conjunto não anulam contraexemplos vermelhos encontrados em revisão.
