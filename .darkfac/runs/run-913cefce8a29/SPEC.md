# SPEC

## Objetivo
Fechar as lacunas restantes do USR-207 (Hub sem queda a cada merge) reaproveitando o que USR-339/348/360/363/365 ja entregaram: imagem GHCR por sha, blue-green no Traefik, classificador de deploy, helper hubNetFetch e sonda externa de /health. Falta (1) o /live ainda usar fetch cru, (2) demandas/roadmap do Hub virem da copia embutida na imagem e envelhecerem quando o deploy e dispensado, (3) o workflow de imagem disparar rebuild em merges so de ledger/docs, (4) nao existir nenhuma leitura de memoria do container nem monitoramento de OOMKilled, e (5) a prova de 1 s de zero downtime depender de um script fora do repositorio e nunca ter sido executada num deploy real.

## Fora de escopo
- Trocar o Dokploy por outra plataforma de deploy ou migrar o compose para Stack/Swarm (OA-057 decidiu blue-green no Traefik)
- Aumentar a VPS ou subir o limite de memoria sem medicao registrada
- Tornar o pacote ghcr.io/gcamposgit/darkhub publico (owner decidiu manter privado com pull autenticado; OA-059 concluida)
- Reimplementar rollback do blue-green (codigo entregue no PR #611) ou reconfigurar autoDeploy, registry GHCR e agendamento da sonda (OA-044/052/053/059 concluidas)
- Editar .github/workflows/* diretamente (caminho protegido em core/orchestrator/guard.py); a mudanca sai como patch + acao do owner

## Desenho
Cinco frentes pequenas e independentes. (a) Frontend: live.html passa a carregar /static/hub_net.js antes de live.js e fetchSnapshot chama window.hubNetFetch preservando AbortController/timeout (AbortError continua propagando sem retry), eliminando a excecao RAW_FETCH_ALLOWED do teste estatico. (b) Frescor de dados sem rebuild: core/owner_actions/remote_ledger.RemoteLedger ganha o caminho do ledger como parametro (default .factory/owner_actions/owner_actions.json, comportamento atual intacto) e o Hub de producao passa a ler .factory/demands/demands.json de origin/main com TTL curto e fallback para o arquivo local, igual ao que USR-348 fez para owner_actions; escrita continua local. (c) Conciliacao de caminhos: deploy_classifier passa a expor a lista unica de caminhos que a imagem embute como runtime (COPY do Dockerfile.hub menos docs/ e .factory/, que ficam so como seed), um checker compara com o trigger de .github/workflows/hub-image.yml e o patch pronto (paths restritos + concurrency) vira proposta + OA, porque o workflow e protegido. (d) Memoria: novo leitor de cgroup v2 (memory.current, memory.max, memory.events oom_kill) com fallback v1, exposto num endpoint que o Hub ja serve, consumido pela sonda externa que ja roda de minuto em minuto fora da VPS para alertar o Telegram ops em oom_kill ou uso alto; mais um script de carga que mede pico e p95 e grava relatorio, com o limite de 512M mudando somente com a medicao registrada (contando a sobreposicao de duas cores do blue-green). (e) Prova: sonda de 1 s versionada no repo (JSONL por amostra + veredito non200==0 e dois shas observados) substituindo o script externo do owner, executada durante um deploy blue-green real, com o relatorio guardado como evidencia.

## Arquivos a tocar
- hub/frontend/live.js
- hub/frontend/live.html
- core/owner_actions/remote_ledger.py
- core/demands/store.py
- hub/backend/service.py
- core/infra/deploy_classifier.py
- core/infra/availability_probe.py
- core/infra/container_memory.py
- scripts/hub_load_probe.py
- scripts/hub_downtime_probe.py
- deploy/dokploy/docker-compose.hub.yml
- docs/proposals/USR-207-zero-downtime.md
- .factory/owner_actions/owner_actions.json
- tests/test_hub_deploy_usr207.py
- tests/js/hub_net.test.cjs

## Riscos
- Rotear o snapshot do /live pelo helper pode somar dois backoffs (o do helper e o polling proprio da pagina) ou engolir o AbortController do timeout de 5 s: usar o helper so no GET do snapshot e cobrir abort com teste node.
- Leitura remota do ledger de demandas depende da API do GitHub e de token para repo privado; qualquer falha precisa cair silenciosamente no arquivo local, senao o Hub perde o roadmap inteiro.
- Teste de carga contra o Hub de producao com limite de 512 MB pode disparar o proprio OOM que queremos medir; comecar com concorrencia baixa, observar oom_kill e abortar na primeira reinicializacao.
- Sobreposicao blue-green dobra a memoria do Hub numa VPS cujos limites somados ja passam da RAM fisica; a medicao pode concluir que o limite precisa cair, nao subir.
- A prova de 1 s exige escrita no Dokploy e uma janela real de deploy; a ferramenta de terminal ja foi negada antes por permissao, e nesse caso a saida e reportar a negacao exata e registrar OA com os comandos absolutos.
- Narrow dos paths do workflow depende do owner publicar o patch (caminho protegido); ate la o workflow continua construindo imagem em merge so de ledger, por isso o teste travado e de superset e nao de igualdade.
