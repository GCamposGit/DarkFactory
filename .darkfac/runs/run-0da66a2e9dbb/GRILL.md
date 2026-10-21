# GRILL

## Premissas
- A demanda é pontual e não exige decisões de negócio, credenciais ou acesso externo.
- O checkout já contém parsing com argparse antes da validação em scripts/line_validate.py e testes específicos em tests/test_usr323_line_validate_cli.py; o planejamento deve verificar essa implementação antes de propor alterações.
- --help e -h devem imprimir uso e encerrar com código 0 antes de consultar Git, criar snapshot ou executar validação.
- Argumentos desconhecidos devem encerrar com código 2 sem efeitos operacionais.
- A chamada sem argumentos deve preservar o fluxo existente, incluindo runner oficial --quick, snapshot para árvore suja e fallback existente, sem reduzir cobertura.
- Esta etapa é exclusivamente de leitura; não foram executados testes, validações ou alterações de arquivos.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
