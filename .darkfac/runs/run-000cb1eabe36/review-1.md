# Review round 1

Verdict: approve

## Nao-bloqueantes
- `core/orchestrator/cloud_coordinator.py:222` — O fallback HostConfig para hosts fora de .factory/config/line_fleet.json usa host_id com a caixa original (não normalizado para lowercase), enquanto verified_capacity é indexado por resolve_host_id() (sempre lowercase). Se um fleet_hosts customizado usar caixa mista para o host local, a capacidade verificada deixaria de ser aplicada a ele. (fix: Normalizar host_config.id para lowercase no fallback (ou comparar com .lower() ao montar verified_capacity/host_states) para manter paridade de case-insensitividade que existia na função removida _local_host_has_capacity.)
