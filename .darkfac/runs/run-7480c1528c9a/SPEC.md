# SPEC

## Objetivo
Implantar um ciclo headless recorrente, reutilizável por projeto, que observe evidências, qualifique achados, deduplique sua admissão na fila única e meça cobertura e custo; o primeiro marco corrige a leitura do Hub e entrega o domínio documental.

## Fora de escopo
- Criar outro agendador, fila, catálogo de projetos, roteador, portal ou caminho de entrega.
- Elevar orçamento, flexibilizar o piso de cota, habilitar hosts desativados, obter novos acessos ou alterar governança protegida.
- Reler o repositório inteiro com LLM a cada rodada, criar achados sem evidência verificável ou inventar limiares de negócio.
- Executar portões completos em paralelo no mesmo host, substituir o portão oficial ou declarar saúde com cobertura parcial, falha ou não aplicabilidade.
- Alterar ou versionar o experimento Canaletto.

## Desenho
Reconciliar primeiro a configuração e a versão implantada do DarkHub e injetar no domínio de demandas o mesmo control store canônico lido pelo BacklogFeeder, preservando 404 apenas para ausência real e propagando indisponibilidade como erro. Em seguida, acrescentar ao control store existente contratos transacionais para política, execução, lease, evidência e chave de achado; não haverá store ou fila paralela. Como FeederCycle apenas admite backlog e Sentinel apenas observa saúde, a inspeção justifica um QualityCycle estreito, chamado pelo loop já existente do CloudCoordinator. Ele calcula rotinas devidas por cadência, evento e vencimento, consolida janelas atrasadas, usa fencing para concorrência e persiste resultados success_no_findings, partial, failed, waiting_resource e not_applicable com cobertura, custo e motivo. Coletores determinísticos produzem evidência normalizada e um fingerprint de código, dependências, regras, versão do coletor e revisão externa; evidência válida evita nova inferência, e LLM só analisa ambiguidade comprovada com contexto e custo limitados. Um achado qualificado usa chave estável e operação atômica para criar ou atualizar um UserTicket de origem agente na fila atual, que segue feeder, roteador, revisão, CI, deploy e rechecagem normais. O primeiro marco entrega reconciliação do Hub, o motor comum e documentação; segurança, eficiência e prova integrada ficam em marcos dependentes.

## Arquivos a tocar
- core/demands/queue_store.py
- core/demands/store.py
- hub/backend/service.py
- hub/backend/api.py
- core/workflow/control_contracts.py
- core/workflow/control_store.py
- core/orchestrator/adapters/control_postgres.py
- core/projects/models.py
- core/line/quality_cycle.py
- core/orchestrator/cloud_coordinator.py
- core/harness/docs_sync.py
- docs/manual/qualidade_continua.md
- docs/FASE_ATUAL.md

## Riscos
- Drift de DSN, variável de ambiente ou imagem implantada pode fazer Hub e feeder consultarem stores distintos; o diagnóstico deve expor apenas backend e SHA sanitizados.
- Migração não atômica entre SQLite e PostgreSQL pode quebrar retomada ou deduplicação; contratos e índices únicos precisam de paridade e rollout compatível.
- Cadências atrasadas ou múltiplos coordenadores podem causar avalanche e tickets repetidos sem due_key, lease e finding_key transacionais.
- Fingerprints incompletos podem reutilizar evidência obsoleta; fingerprints excessivos podem desperdiçar inferência e cota.
- Coletores documentais, de advisories e telemetria podem gerar falso positivo quando a cobertura é insuficiente; esses casos devem permanecer partial ou not_applicable.
- Queda de host ou indisponibilidade de fonte externa pode confundir espera com falha e perder trabalho se o estado for apenas em memória.
- Adicionar testes sem consolidar casos existentes viola o orçamento da suíte; cada ticket deve substituir ou parametrizar cobertura redundante.
