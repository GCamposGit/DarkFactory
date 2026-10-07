# Relatório de Entrega - USR-122

## Resumo da Demanda
- **Ticket**: USR-122
- **Título**: VPS sem espaco em disco derruba o build do darkfac-cloud
- **Status**: Completed

## Contexto & Causa Raiz
Em 01/10 18:19 UTC o deploy do `darkfac-cloud` (USR-121, PR #111) falhou durante o build devido ao erro do apt-get:
`You don't have enough free space in /var/cache/apt/archives/`.
O VPS Hetzner CX23 possui capacidade de disco limitada e acumula cache de buildkit do Docker (`cleanDockerBuilder`) e camadas/imagens órfãs (`cleanUnusedImages`). Anteriormente não havia rotina automática de limpeza segura nem alerta proativo de saturação de disco.

## Solução Implementada

1. **Módulo Central de Limpeza Segura & Monitoramento de Disco (`core/infra/vps_cleanup.py`)**:
   - `clean_vps_docker_cache(api_url, api_key, transport=...)`: Aciona `POST /api/settings.cleanDockerBuilder` e `POST /api/settings.cleanUnusedImages` via API do Dokploy.
     - **Contrato Estrito de Segurança**: NUNCA remove volumes Docker nem containers em execução. Limpa unicamente cache dangling de build e imagens sem uso.
   - `is_disk_space_failure(error_text)`: Detecta padrões de esgotamento de disco (`You don't have enough free space`, `no space left on device`, `ENOSPC`, `/var/cache/apt/archives`, etc.).
   - `check_vps_disk_and_alert(disk_percent, threshold=85.0)`: Despacha alerta crítico estruturado via `NotificationService` (canal Telegram ao bot do Owner) quando a ocupação do disco excede 85%.
   - `get_local_disk_usage(path)`: Coleta volumetria do filesystem (`total_gb`, `used_gb`, `free_gb`, `disk_percent`) usando `shutil.disk_usage` sem dependências externas.
   - `clean_vps_if_configured()`: Resolve credenciais Dokploy com fallback seguro e executa a limpeza segura.

2. **Diagnóstico no Deployer Dokploy (`scripts/dokploy_redeploy.py`)**:
   - Adicionada flag `--clean`: Executa limpeza segura do builder cache e imagens não utilizadas no Dokploy antes de disparar o redeploy.
   - `detect_disk_space_failure(deployment)`: Analisa mensagens de erro, descrições e títulos de deploys com falha para detectar esgotamento de disco e emitir diagnóstico claro e orientações de remediação.
   - Monitoramento contínuo no health check: Detecta uso de disco acima de 85% e alerta o owner bot.

3. **Telemetria de Disco no Backend DarkHub (`hub/backend/main.py`)**:
   - O endpoint `/health` agora reporta `disk_percent`, `disk_free_gb` e `disk_total_gb` em tempo real para sincronização de nós e diagnósticos.

4. **Agendamento no Daemon de Backup Autônomo (`core/infra/backup_cron.py`)**:
   - Integrada a etapa 4 de limpeza segura periódica do cache de Docker na rotina autônoma de manutenção e backup 3-camadas.

5. **Suíte de Testes Dedicada (`tests/test_vps_cleanup.py`)**:
   - Cobertura de 10 testes cobrindo todas as assinaturas de erro de disco, endpoints de limpeza Dokploy, tratamento de falhas parciais, limiares de alerta (> 85%), resolução de credenciais e flags de CLI.

## Validações Executadas
- `tests/test_vps_cleanup.py`: 10 passed (100% de sucesso).
- `tests/test_dokploy_redeploy.py`: 78 passed, 1 skipped.
- `tests/test_infra08_three_tier_backup.py`: 18 passed, 1 skipped.
- `tests/test_no_tracked_secrets.py`: 43 passed.
