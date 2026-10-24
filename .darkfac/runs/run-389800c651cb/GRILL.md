# GRILL

> Decisoes adotadas da tentativa anterior do mesmo ticket (run run-99c42c8f36de); o owner nao foi consultado de novo.

## Premissas
- Esta rodada é somente leitura: não executar deploys, testes que gravem estado nem modificar arquivos.
- Tratar a demanda como continuidade do USR-207 e dos tickets relacionados, aproveitando o que já existe e verificando as lacunas antes de planejar novas alterações.
- O congelamento de 11/10 deixou de restringir a demanda conforme o contrato pós-V0 de 17/10.
- Preservar a decisão já registrada em OA-057: blue/green via Traefik no Dokploy, sem migração para Swarm.
- Reutilizar o workflow GHCR, o helper hubNetFetch, o classificador de deploy e a sonda externa existentes; sua presença no código não comprova aceite operacional.
- Conciliar os caminhos realmente copiados pelo Dockerfile com o workflow e o classificador; dados necessários ao Hub devem continuar atualizados quando o deploy da imagem for dispensado.
- Interpretar no máximo um deploy por merge como uma única operação lógica de atualização, permitindo as fases internas de sobreposição e retirada do blue/green.
- Validar zero indisponibilidade por HTTPS no domínio público, com sonda externa de 1 segundo atravessando a troca e confirmando que a nova versão passou a servir; timeout ou erro de conexão também contam como falha.
- Centralizar chamadas HTTP no helper, evitando repetição automática de operações mutáveis sem garantia de idempotência e preservando dados não enviados durante a recuperação.
- Reutilizar o destino Telegram ops e o agendamento externo já registrados, verificando sua operação sem pedir novamente configurações concluídas.
- Manter inicialmente 512 MB; medir carga e sobreposição dos containers, monitorar OOMKilled e ajustar o limite com evidência, sem ampliar a VPS nesta demanda.

## Decisoes do owner
- [business] “CI publica no GHCR” significa publicar a imagem com acesso público ou manter o pacote privado com pull autenticado pela VPS? O repositório registra pacote privado e falha de pull no USR-363; a imagem inclui código e dados de .factory/. -> **Manter imagem privada e corrigir autenticação de pull** (respondida pelo owner)
