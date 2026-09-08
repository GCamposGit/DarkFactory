# Pesquisa de arquitetura — workflow híbrido DarkFac

Consulta: 08/09/2026. Escopo: fundamentar planejamento, sem executar benchmark de runtimes.

## Síntese

A proposta preserva o domínio DarkFac e compara uma biblioteca durável sobre PostgreSQL com o runtime local já entregue. DBOS é o candidato inicial por compatibilidade de stack e superfície operacional reduzida. A escolha exige spike; não há medição local de consumo ou custo que confirme a preferência. Temporal e Prefect são alternativas documentadas; LangGraph e n8n têm papéis possíveis dentro de etapas e integrações.

Separar política, execução durável, worker, integração e publicação evita atribuir ao modelo autoridade de avançar o fluxo. Os contratos de idempotência, evidência e aprovação continuam sendo responsabilidades da aplicação, qualquer que seja a ferramenta.

## Fontes primárias consultadas

| Fonte | Fato usado | Aplicação proposta |
| --- | --- | --- |
| [DBOS Architecture](https://docs.dbos.dev/architecture) | Persistência sobre PostgreSQL; recuperação distribuída demanda coordenação; sem servidor de orquestração separado. | Avaliar coordenador único cloud e workers externos, evitando depender de HA não necessária. |
| [DBOS Workflows](https://docs.dbos.dev/python/tutorials/workflow-tutorial) | Workflow deve ser determinístico; operações não determinísticas ficam em steps. | Persistir outputs de IA e efeitos, não reexecutar raciocínio durante replay sem necessidade. |
| [DBOS Communication](https://docs.dbos.dev/python/tutorials/workflow-communication) | Comunicação com workflows duráveis. | Validar espera/retomada humana no spike. |
| [DBOS Queues](https://docs.dbos.dev/python/reference/queues) | Filas de execução duráveis e controle de concorrência. | Avaliar limites por execução/capacidade. |
| [Temporal Deployment](https://docs.temporal.io/self-hosted-guide/deployment) | Serviço e gestão de schemas próprios. | Comparar custo operacional total, sem usar servidor de desenvolvimento como solução de produção. |
| [Prefect Server](https://docs.prefect.io/v3/concepts/server) | Servidor self-hosted, persistência e banco PostgreSQL suportado. | Alternativa documental à escolha inicial. |
| [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) | Persistência de execução de grafos. | Adaptador agêntico eventual; não presumir controle completo do SDLC. |
| [n8n Editions](https://docs.n8n.io/deploy/host-n8n/community-edition-features) | Community gratuita; ambientes/Git não integram essa edição. | Usar exportação e manutenção externa versionada, com owner único. |
| [n8n Docker Compose](https://docs.n8n.io/deploy/host-n8n/install-options/install-using-docker-compose) | Método atual recomendado de instalação. | Preparar instalação simples no Dokploy, após reconciliar eventual instância existente. |
| [Dokploy Auto Deploy](https://docs.dokploy.com/docs/core/auto-deploy) | Webhooks/API para deploy. | Separar staging automático de promoção de produção protegida. |
| [GitHub Environments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments) | Regras de ambiente dependem do plano e da visibilidade. | Não pressupor reviewers de ambiente disponíveis em repositório privado; fallback de política no serviço de release. |
| [OpenRouter Limits](https://openrouter.ai/docs/api_reference/limits) | Saldo e limite de chave distintos; usage acumulado separado de usage_monthly. | Reconciliar rótulo financeiro do Hub antes de usar métricas no despacho. |
| [Telegram Bot API](https://core.telegram.org/bots/api) | Polling, atualizações e callbacks. | Canal de comandos do owner com identidade, deduplicação e continuidade no Hub. |
| [OWASP ASVS](https://owasp.org/www-project-application-security-verification-standard/) | Referência de verificação de segurança de aplicações. | Perfil de controles proporcional a produto e risco. |
| [SLSA](https://slsa.dev/spec/v1.2/) | Referência de proveniência na cadeia de software. | Origem/artefato verificáveis; sem declaração de certificação. |

## Incertezas registradas

- DBOS não foi instalado nem comparado em execução nesta sessão. HF-02 decide.
- URLs antigas da documentação n8n retornaram páginas ausentes; foram substituídas por caminhos atuais do site oficial. A página Docker antiga identifica Docker Compose como método recomendado.
- n8n aparece no catálogo local, mas sem URL de instância; Dokploy apresentou tela de login. Instalação não confirmada.
- Hub mostrou crédito OpenRouter de US$ 8,59. O rótulo mensal de US$ 1,41 não foi aceito como prova de consumo mensal: leitura focal identificou uso de campo acumulado.
- Infraestrutura e relatórios têm divergências de versão/estado. O plano usa reconciliação como primeiro gate, sem alterar a execução existente.

Plano derivado: [HYBRID_WORKFLOW_PLAN_2026-09-08.md](../../../docs/HYBRID_WORKFLOW_PLAN_2026-09-08.md).
