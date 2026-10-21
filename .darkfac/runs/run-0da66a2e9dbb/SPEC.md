# SPEC

## Objetivo
Garantir que ajuda e argumentos inválidos de line_validate encerrem antes de qualquer operação, preservando a validação sem argumentos.

## Fora de escopo
- Alterar semântica ou cobertura do portão oficial.
- Alterar snapshots, sanitização de ambiente, despacho remoto ou fallback.

## Desenho
A leitura confirmou argparse em build_parser(), parsing no início de main() e encaminhamento de sys.argv[1:] no entrypoint. Os testes existentes cobrem ajuda, argumento desconhecido e encaminhamento ao runner em árvore limpa. Um único ticket deve complementar a cobertura do entrypoint e dos fluxos sem argumentos, ajustando produção apenas se os testes revelarem lacuna. Nenhum teste ou gate foi executado nesta etapa de leitura.

## Arquivos a tocar
- tests/test_usr323_line_validate_cli.py
- scripts/line_validate.py

## Riscos
- Testes restritos a main() podem deixar passar regressão no encaminhamento dos argumentos pelo entrypoint.
- Executar o wrapper sem isolamento durante testes pode criar snapshots e despachar a suíte real.
- Mocks que verificam apenas a chamada ao runner podem não detectar perda de --quick ou alteração dos códigos de saída.
