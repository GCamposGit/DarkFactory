# GRILL

## Premissas
- OA-042 resolve as decisões de produto e destino: pilot-hello privado, texto 'DarkFac pilot', Dokploy existente em projeto separado de darkfac-core, retenção arquivada e build no venv.
- OA-043 consta aberta no registro consultado; a existência e os identificadores da aplicação, domínio e webhook deverão ser verificados antes da execução de M5, reutilizando essa dependência sem solicitar novamente acessos ou segredos neste grill.
- O planejamento reutilizará as evidências de M1 e M2 e o descritor existente do projeto, cujo comando de testes é validate; não recriará repositório ou infraestrutura.
- O deploy usará a configuração específica do pilot-hello e o adaptador Dokploy existente, preservando o isolamento de darkfac-core e o allowlist do script de redeploy do núcleo.
- O SHA completo resultante do merge de M4 será a referência esperada. HTTP 200 e versão semver isolada não comprovam correspondência: a identidade servida por /healthz deverá permitir verificar esse SHA explicitamente.
- O smoke deverá verificar HTTP 200, status ok e identidade correspondente ao SHA mergeado; divergência ou ausência de identidade impedirá declarar M5 concluído.
- A evidência vinculada ao ticket do piloto conterá URL efetivamente consultada, SHA esperado e observado, saída do smoke e horário UTC, sem credenciais.
- OA-047 permanece responsável pela entrada real via Telegram; esta etapa limita-se ao grill em modo somente leitura, sem implementação, deploy ou alteração de registros.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
