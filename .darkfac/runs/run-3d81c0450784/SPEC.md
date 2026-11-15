# SPEC

## Objetivo
Entregar o primeiro marco da operação do domínio de avaliação contínua de vulnerabilidades de dependências no portfólio: um perfil de habilitação explícito por projeto (perfil + acesso + destino durável de evidência + limites de custo + prazo de reteste) com portão fail-closed, motivo visível por projeto e alertas acionáveis sem repetição inalterada. Nenhum projeto é avaliado sem habilitação explícita; elegibilidade de cadastro no BacklogFeeder não habilita o domínio.

## Fora de escopo
- Motor de varredura/avaliação de vulnerabilidades, consultas a advisories e deduplicação de achados (marcos seguintes)
- Métricas de cobertura (avaliado/desconhecido/omitido/evidência vencida) e de custo medido por execução
- Painéis novos no DarkHub e isolamento visual de evidência por projeto
- Execução das provas periódicas de reteste e demonstração real ponta a ponta em darkfac
- Campanhas ofensivas, remediação automática e pedido de segredos novos
- Reconciliação da divergência de USR-449 e da baseline de QualityCycle/core/security (marco próprio)

## Desenho
Contratos Pydantic v2 novos em core/portfolio/models.py (DependencyWatchProfile, DependencyWatchStatus, DependencyWatchEligibility) e um módulo de política em core/portfolio/dependency_watch.py, carregando .factory/config/dependency_watch.json (sem segredos; destino durável por referência, nunca valor). O carregador valida contra ProjectRegistry (core/projects/registry.py) e falha fechado: projeto sem perfil, sem acesso concedido, sem destino durável autorizado, sem limite de custo por projeto ou sem prazo de reteste fica blocked com código de motivo estruturado, nunca silenciado. O resolvedor de elegibilidade combina o perfil, os tetos globais já existentes (PortfolioBudgetManager em core/portfolio/budget_manager.py) e a validade da última prova (prazo por perfil; evidência vencida vira pendência, não ausência de vulnerabilidade) e expõe CLI read-only `python -m core.portfolio.dependency_watch --json` com uma linha por projeto. Alertas reutilizam NotificationService/NotificationStore (DarkHub) e TelegramGateway (core/integrations/telegram.py) com fingerprint determinístico por (projeto, tipo, motivo), de modo que apenas novidade acionável, falha ou prazo vencido notifica; estado inalterado não repete. Retenção é permanente: nada é apagado por idade e, sem destino durável autorizado no perfil, o projeto fica blocked e nenhum dado dele é gravado.

## Arquivos a tocar
- core/portfolio/models.py
- core/portfolio/dependency_watch.py
- .factory/config/dependency_watch.json
- core/portfolio/budget_manager.py
- core/notifications/service.py
- docs/manual/portfolio.md
- docs/FASE_ATUAL.md
- tests/test_dependency_watch_profile.py
- tests/test_dependency_watch_eligibility.py
- tests/test_dependency_watch_alerts.py

## Riscos
- Base anterior inconsistente (QualityCycle e core/security inexistentes, divergência de USR-449): o marco deve ficar autocontido em core/portfolio e não presumir componentes ausentes
- Confundir elegibilidade de cadastro (BacklogFeeder/ProjectRegistry) com habilitação do domínio, avaliando projeto sem acesso explícito
- Destino durável ausente levar a gravar evidência em local não autorizado em vez de bloquear o projeto
- Ruído de alerta: sem fingerprint estável, DarkHub e Telegram repetem o mesmo estado a cada ciclo
- Interação com os tetos globais de custo: limite por projeto permissivo não pode furar o teto global existente
