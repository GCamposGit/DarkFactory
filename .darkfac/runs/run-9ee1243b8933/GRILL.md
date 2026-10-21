# GRILL

> Decisoes adotadas da tentativa anterior do mesmo ticket (run run-1d143cbade49); o owner nao foi consultado de novo.

> Decisoes adotadas da tentativa anterior do mesmo ticket (run run-0983a90d6be0); o owner nao foi consultado de novo.

> Decisoes adotadas da tentativa anterior do mesmo ticket (run run-8593ca777615); o owner nao foi consultado de novo.

> Decisoes adotadas da tentativa anterior do mesmo ticket (run run-5c5856ae065f); o owner nao foi consultado de novo.

> Decisoes adotadas da tentativa anterior do mesmo ticket (run run-294bd6f77095); o owner nao foi consultado de novo.

> Decisoes adotadas da tentativa anterior do mesmo ticket (run run-5075f05a7ea4); o owner nao foi consultado de novo.

## Premissas
- OA-042 resolveu D1–D5: pilot-hello, texto 'DarkFac pilot', Dokploy existente opção A, manter arquivado e build no venv; essas decisões não serão perguntadas novamente.
- O escopo segue docs/plans/USR-279-greenfield-pilot.md: página estática única e /healthz, Python 3.12+, biblioteca padrão no runtime, sem banco, autenticação ou novas contratações.
- O repositório é privado em GCamposGit/pilot-hello; a opção A permite publicação pelo domínio da infraestrutura existente, em projeto Dokploy separado de darkfac-core.
- M1 e M2 possuem evidências registradas; o planejamento deve verificar e reutilizar esses resultados, retomando os marcos restantes sem recriar recursos.
- Acessos e configuração de deploy/webhook já estão acompanhados pela OA-043; a entrada real pelo Telegram está acompanhada pela OA-047. São dependências existentes, sem necessidade de repetir perguntas neste grill.
- Os marcos M1–M7 mantêm até três critérios cada; conclusão exige evidências reais de revisão independente, PR, CI, merge, deploy e smoke vinculadas ao SHA.
- A independência será demonstrada por clone limpo fora do checkout DarkFac, com instalação, testes, wheel e execução sem imports ou serviços do core; o comando de testes registrado no descritor é validate.
- Esta etapa é somente leitura e preparação do grill; não implementa, publica nem altera arquivos.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
