# Roadmap de Infraestrutura & Escalabilidade Multi-Projeto

> **Módulo**: `core.infra`  
> **Última Atualização**: 2026-09-26  
> **Status Geral**: 100% Concluído no Mundo Real (Fases 1, 2, 3 e 4 Operacionais)

---

## Sumário Visual das Fases

```
[FASE 1: CONCLUÍDA 🟢]           [FASE 2: CONCLUÍDA 🟢]           [FASE 3: CONCLUÍDA 🟢]           [FASE 4: CONCLUÍDA 🟢]
+--------------------------+     +--------------------------+     +--------------------------+     +--------------------------+
| Mapeamento & Topologia   |     | Cloud VPS & Dokploy PaaS |     | Postgres NVMe & Backups  |     | CI Headless & Deploys    |
| - Mapeamento de nós/GPUs |     | - Hetzner CPX21 / CX23   |     | - Multi-Tenant DBs       |     | - Git Webhook Deploys    |
| - Tailscale Mesh Ativo   | ==> | - Dokploy Orchestrator   | ==> | - Cloudflare R2 Backups  | ==> | - Runner Local On-Prem   |
| - Docker On-Prem (E: 3TB)|     | - Cloudflare Tunnels WAF |     | - Espelho Noturno 3TB    |     | - Multi-Projetos Paral.  |
| - Google Remote Desktop  |     | - Zero Open Ports        |     | - RPO < 24h Custo Zero   |     | - Monitoramento Métricas |
+--------------------------+     +--------------------------+     +--------------------------+     +--------------------------+
```

---

## Detalhamento dos Itens do Roadmap

### Fase 1: Mapeamento, Conexão Segura & Ativação On-Premise [CONCLUÍDA 🟢]

| ID | Item | Responsável | Status | Entregas & Evidências |
| :--- | :--- | :--- | :--- | :--- |
| **INFRA-01** | **Mapeamento de Hardware e Topologia Híbrida** | DarkFac Core | ✅ **DELIVERED** | Criação de `core/infra/`, `models.py`, `inventory.py`, `cli.py` e `.factory/infra/inventory.json`. Diagnóstico técnico comprovando uso da RTX 4070 para IA e do i7-4790K para storage/CI. |
| **INFRA-02** | **Rede Mesh Privada Zero-Trust (Tailscale)** | Operador & Core | ✅ **DELIVERED** | Malha ativa. Nós integrados sob a mesma conta: `ai-notebook` (`100.81.84.124`, Win11) e `desktop-g45ipem` (`100.78.181.90`, Win10). Ping e rota validados. |
| **INFRA-03** | **Ativação do Docker no Servidor On-Premises (Anti-Disco Cheio)** | Operador & Core | ✅ **DELIVERED** | Docker Desktop instalado no `desktop-g45ipem` com backend WSL2. Repositório de imagens e volumes alocado no **Drive E: (HD de 3 TB)**, preservando 100% do SSD de 240 GB. Validação com `hello-world`. |
| **INFRA-04** | **Acesso Remoto Operacional & Headless** | Operador | ✅ **DELIVERED** | Google Remote Desktop configurado e operacional para gerenciamento visual do PC sem necessidade de periféricos físicos dedicados. |

---

### Fase 2: Expansão Cloud VPS & Orquestração PaaS [CONCLUÍDA 🟢]

| ID | Item | Pré-requisito | Status | Entregas & Critérios de Aceite |
| :--- | :--- | :--- | :--- | :--- |
| **INFRA-05** | **Provisionamento da Cloud VPS Primária (Hetzner CX23)** | - | ✅ **DELIVERED** | Servidor **`darkfac-vps-primary`** (#165058444) provisionado no Datacenter Falkenstein (Alemanha). IPv4: `178.105.73.168`, IPv6: `2a01:4f8:c014:634::/64`. 2 vCPUs, 4 GB RAM, 40 GB NVMe, 20 TB tráfego por ~US$ 7.19/mês. |
| **INFRA-06** | **Instalação do Dokploy PaaS & Tailscale Mesh na VPS** | INFRA-05 | ✅ **DELIVERED** | Dokploy PaaS instalado com sucesso na Hetzner CX23; ouvindo na porta 3000; Tailscale ativo no IP privado `100.83.176.60`. Conexão ponto a ponto testada e validada. |
| **INFRA-06B** | **Subdomínio Oficial com SSL (dokploy.ggcampos.com)** | INFRA-06 | ✅ **DELIVERED** | Apontamento DNS Cloudflare A para 178.105.73.168. Certificado SSL automático Let's Encrypt emitido e validado com sucesso (HTTP/1.1 200 OK em https://dokploy.ggcampos.com). |

---

### Fase 3: Dados Multi-Projeto & Política de Resiliência [CONCLUÍDA 🟢]

| ID | Item | Pré-requisito | Status | Critérios de Aceite |
| :--- | :--- | :--- | :--- | :--- |
| **INFRA-07** | **PostgreSQL Multi-Tenant em Armazenamento NVMe** | INFRA-06 | ✅ **DELIVERED** | Instância PostgreSQL 16+ provisionada no Dokploy sobre NVMe de alta performance. Bancos e credenciais isolados por projeto com custo adicional zero. |
| **INFRA-08** | **Automação de Backups 3-Camadas (R2 + On-Premise)** | INFRA-07 | ✅ **DELIVERED** | Rotina automática de dump compactado e criptografado (AES-256-GCM) para Cloudflare R2 com espelho noturno para o Drive `E:` (3 TB) do `desktop-g45ipem`, retenção assimétrica (7d R2 / 120d on-prem) e restore drills automáticos em sandbox (`core.infra.backup_cron`, `core.infra.backup_service`). |

---

### Fase 4: Automações Avançadas & Escala Multi-Projeto [CONCLUÍDA 🟢]

| ID | Item | Pré-requisito | Status | Critérios de Aceite |
| :--- | :--- | :--- | :--- | :--- |
| **INFRA-09** | **Pipelines de Deploy Contínuo (Git Push Webhooks & Dokploy Gateway USR-18)** | INFRA-06 | ✅ **DELIVERED** | Webhooks configurados e testados com verificação HMAC-SHA256, deduplicação idempotente, avaliação de entrega DF-20 e trigger de auto-deploy Dokploy na Hetzner VPS. |
| **INFRA-10** | **Runner de CI e Batch Workloads no Servidor On-Premises (USR-16 / HF-27-11)** | INFRA-03 | ✅ **DELIVERED** | Execução headless de suítes de validação e CI no i7-4790K com despacho remoto via Tailscale (HF-27-11 / USR-16), liberando recursos do notebook de desenvolvimento. |
| **INFRA-11** | **Painel Unificado de Métricas de Infraestrutura no DarkHub (Hardware & Contêineres)** | INFRA-01 | ✅ **DELIVERED** | Telemetria ao vivo de CPU, RAM e Disco via `psutil`, descoberta de contêineres Dokploy e Docker em tempo real, endpoints `/api/infra/metrics` e painel unificado no DarkHub (`core.infra.metrics`, `hub/frontend/infra.js`). |

---

## Registro de Decisões Arquiteturais Vinculadas (ADRs)

- [`ADR-001`](file:///c:/dev/DarkFac/.factory/infra/decisions/ADR-001-hybrid-multi-project-topology.md): Topologia Híbrida em 3 Camadas (Dev Core + Storage On-Premise + Cloud VPS).
- [`ADR-002`](file:///c:/dev/DarkFac/.factory/infra/decisions/ADR-002-vps-and-paas-orchestration.md): Seleção de VPS & Dokploy PaaS para Orquestração de Containers.
- [`ADR-003`](file:///c:/dev/DarkFac/.factory/infra/decisions/ADR-003-database-and-backup-strategy.md): Banco de Dados Multi-Tenant & Backups Off-Site R2 a Custo Zero.
- [`ADR-004`](file:///c:/dev/DarkFac/.factory/infra/decisions/ADR-004-secure-networking-and-edge.md): Rede Blindada Zero-Trust com Portas 100% Fechadas.

---

## Inventário Atual dos Nós Conectados

```powershell
# Inspecione o status atualizado no terminal:
python C:\dev\DarkFac\core\infra\cli.py status
python C:\dev\DarkFac\core\infra\cli.py list
```

- **`ai-notebook`** (`100.81.84.124`): Windows 11 25H2 | 32 GB RAM | RTX 4070 8 GB | Antigravity IDE | Status: **ACTIVE 🟢**
- **`desktop-g45ipem`** (`100.78.181.90`): Windows 10 22H2 | 16 GB RAM | Docker Desktop no Drive E: (3 TB) | Google Remote Desktop | Status: **ACTIVE 🟢**
- **`darkfac-vps-primary`** (`100.83.176.60` / `178.105.73.168`): Hetzner CX23 | Dokploy PaaS | PostgreSQL 16 NVMe | Status: **ACTIVE 🟢**
- **`cloudflare-edge`**: DNS, WAF, Tunnels | Status: **ACTIVE 🟢**
