# GRILL

## Premissas
- Não explorei o repositório nesta rodada. As premissas e decisões do owner já fixam retenção, limites de custo e canais de alerta, então não há pergunta bloqueante.
- Um projeto habilitado exige perfil e acesso explícitos. Sem eles, o projeto fica bloqueado com motivo visível e não entra nas métricas como avaliado.
- Os contadores por projeto são quatro, sempre sobre o universo de controles e dependências do perfil: avaliado, desconhecido, omitido e evidência vencida. Cada item cai em exatamente uma categoria, com precedência fixa: vencido, depois omitido, depois desconhecido, depois avaliado.
- Avaliado significa que há versão resolvida de ambiente identificado (lockfile, imagem ou venv instalado) e consulta bem-sucedida à fonte de advisories dentro da validade. Requisito declarado (como `requirements.txt` sem pin) sem versão resolvida conta como desconhecido, nunca como avaliado.
- Omitido é o que o perfil exclui por escolha explícita, com motivo registrado. Falha de fonte, timeout ou ausência de advisory em fonte sem cobertura do ecossistema conta como desconhecido ou falha, nunca como limpo.
- A validade da evidência vem do prazo por perfil, já decidido. Evidência vencida perde validade e aparece como pendência, sem herdar o resultado anterior.
- A proveniência de cada métrica registra a fonte (nome, versão ou data do snapshot de advisories, hora da consulta), o ambiente (identificador, método de resolução, hash do lockfile ou da imagem) e o commit avaliado.
- A superfície é uma biblioteca em `core/` com regra de negócio, mais uma CLI headless com saída JSON UTF-8. A CLI aceita filtro por projeto e retorna código de saída distinto para falha de fonte. Não há regra de negócio em UI. O painel do DarkHub consome a biblioteca depois e não faz parte do núcleo desta demanda.
- As métricas respeitam o isolamento por projeto e a retenção permanente no destino durável do perfil. Sem destino autorizado, o armazenamento desse projeto aguarda definição.
- Limiares de cobertura (por exemplo, percentual mínimo aceitável) ficam fora desta entrega. Dependem de baseline e de decisão futura do owner. A demanda só mede e expõe.
- Alertas seguem a decisão do owner: DarkHub e Telegram, só para novidade acionável, falha ou prazo vencido, sem repetir alerta inalterado.
- A demonstração inicial roda em darkfac com integração real de ponta a ponta. Mock isolado não conta como entrega.
- A divergência sobre USR-449 e a ausência de `QualityCycle` e `core/security` nos caminhos inspecionados exigem reconciliação na etapa de planejamento antes de criar componentes novos.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
