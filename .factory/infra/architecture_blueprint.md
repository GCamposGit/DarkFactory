# Dark Factory Infrastructure Blueprint: Mapeamento, Pesquisa & Arquitetura Híbrida Multi-Projeto

> **Status**: Ativo / Aprovado  
> **Data de Emissão**: 2026-09-07  
> **Autor**: Dark Factory Orchestration Core (`core.infra`)  
> **Objetivo**: Estruturar a infraestrutura com excelente custo-benefício e alta escalabilidade para viabilizar a gestão paralela de múltiplos projetos em produção com segurança rigorosa e automação contínua.

---

## 1. Mapeamento e Diagnóstico da Infraestrutura Existente

### 1.1 Estação de Trabalho Principal (Dev Core)
- **Equipamento**: Notebook Predator Helios Neo 16.
- **Especificações**:
  - **Processador**: Intel Core i7 moderno de alto desempenho (13ª/14ª Geração HX).
  - **Memória RAM**: 32 GB DDR5.
  - **GPU**: NVIDIA GeForce RTX 4070 Laptop (8 GB GDDR6, arquitetura Ada Lovelace, Compute Capability 8.9).
  - **Armazenamento**: NVMe SSD 1 TB.
- **Diagnóstico & Papel Estratégico**:
  - **Inference Local de Alta Velocidade**: Graças à arquitetura Ada Lovelace e núcleos tensores modernos, esta máquina é a **campeã de velocidade para IA local**. Executa perfeitamente Ollama com modelos quantizados (Qwen 2.5 7B/14B, Llama 3.3 8B) e `faster-whisper` em CUDA FP16 nativo.
  - **Papel**: Núcleo de desenvolvimento, pair programming com agentes, testes unitários locais e orquestração da fábrica.

---

### 1.2 Servidor On-Premises Dedicado
- **Equipamento**: Servidor Customizado LGA1150.
- **Especificações**:
  - **CPU**: Intel Core i7-4790K (4 núcleos / 8 threads @ 4.0 GHz base / 4.4 GHz turbo, Haswell).
  - **Cooler**: Corsair H110 Liquid Cooler 94 CFM (watercooler de 280mm duplo).
  - **Placa-mãe**: Gigabyte GA-Z97X-SOC FORCE (placa enthusiast para overclock e multi-GPU).
  - **Memória RAM**: 16 GB (2x 8 GB) DDR3-2400 G.Skill Trident X CL10.
  - **Armazenamento Interno**: Kingston SSDNow V300 240 GB (boot rápido) + Hitachi 3 TB 7200 RPM HDS723030ALA640 (alta densidade).
  - **Armazenamento Externo**: Samsung P3 1 TB USB 3.0.
  - **Placas de Vídeo (Dual-GPU)**: 2x Zotac AMP Extreme GeForce GTX 980 Ti 6 GB GDDR5 (arquitetura Maxwell, Compute Capability 5.2).
  - **Gabinete & Alimentação**: Corsair Graphite Series 730T Full Tower + Fonte Corsair AX1200 (1200W 80+ Gold Fully Modular).
  - **Periféricos de Diagnóstico**: Monitor Samsung LU28E590DS/ZA 28'' 4K + Headset HyperX Cloud Revolver.

#### Diagnóstico Técnico & Recomendações Realistas para o On-Premises:
1. **O Mito das GPUs Maxwell (GTX 980 Ti) para LLMs Modernos**:
   - As 980 Ti possuem **Compute Capability 5.2**. Os motores modernos de IA (Ollama, llama.cpp, PyTorch moderno, vLLM) descontinuaram suporte otimizado para arquiteturas anteriores a Pascal (CC 6.0) ou Ampere/Ada. Elas não têm suporte nativo a operações FP16/BF16/FP8 aceleradas por Tensor Cores.
   - Além disso, sob carga total, as duas GTX 980 Ti consomem juntas **~500W a 600W de energia elétrica**, gerando calor intenso e alta conta de luz, entregando um throughput em LLMs muito inferior à RTX 4070 de 115W do notebook.
2. **O Papel Ideal de Altíssimo Valor**:
   - **Vault Local de Storage & Backups (3 TB + 1 TB)**: Com o HDD Hitachi de 3 TB e o Samsung de 1 TB, este servidor é a fortaleza ideal para **Cold Storage Local**, espelhamento de dumps de bancos de dados PostgreSQL, volumes de Docker e histórico de artefatos.
   - **Headless Build / CI / Staging Worker**: Com a CPU i7-4790K (4.0 GHz) e watercooler H110, é uma máquina estável e rápida para rodar compilações pesadas de Docker, suítes longas de testes de integração, workers Celery/Redis e pipelines batch sem travar o notebook de trabalho.
   - **Sistema Operacional Recomendado**: **Ubuntu Server 24.04 LTS Headless** ou **Proxmox VE 8** (se desejar gerenciar máquinas virtuais e containers LXC com facilidade de snapshots).
   - **Dica de Energia**: Remover ou desativar uma das 980 Ti (ou ambas se não forem necessárias para render/gráficos legacy) reduz o consumo em idle de ~150W para ~35-45W, economizando energia sem perder capacidade de servidor.

---

### 1.3 Contas e Serviços de Nuvem Atuais
- **Cloudflare**:
  - **Status**: Ativo para DNS e roteamento de e-mail.
  - **Valor Estratégico**: É a peça-chave de segurança. Com **Cloudflare Tunnels (`cloudflared`)**, podemos publicar serviços da VPS e do servidor On-Premise para a web com SSL automático, proteção contra DDoS e autenticação segura (Zero Trust / Access) **sem precisar abrir nenhuma porta no roteador de casa e sem pagar por IP fixo**.
- **Hostinger**:
  - **Status**: Conta simples de hospedagem de site.
  - **Papel**: Hospedagem de landing pages estáticas, sites WordPress de marketing e gestão de domínios.
- **n8n**:
  - **Status**: Conta ativa para automação de workflows.
  - **Papel**: Orquestrador de integrações com APIs externas, webhooks e automações de negócio.
- **Google Cloud / Google Drive**:
  - **Status**: Conta pessoal ativa.
  - **Papel**: Identity Provider (OAuth para Cloudflare Access) e destino secundário de cold storage para backups essenciais via Google Drive API / rclone.

---

## 2. Pesquisa de Soluções & Comparativo (2026)

### 2.1 Comparativo de Provedores de VPS

| Provedor | Plano Típico | vCPU / RAM / NVMe | Custo Estimado | Vantagens | Desvantagens |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Hetzner Cloud** *(Top Pick)* | CPX21 ou CPX31 | 3-4 vCPU AMD, 4-8 GB RAM, 80-160 GB NVMe | ~€8 a €15/mês (~US$ 9-16) | Melhor relação custo-desempenho da indústria, rede 20 TB, snapshots fáceis, datacenters rápidos (Ashburn/EUA, Alemanha). | Suporte é puramente técnico (unmanaged), verificação de conta rigorosa. |
| **Hostinger KVM VPS** *(Conveniência)* | KVM 2 ou KVM 4 | 2-4 vCPU, 8-16 GB RAM, 100-200 GB NVMe | ~US$ 7 a US$ 13/mês | Aproveita o faturamento e painel já existentes na Hostinger, suporte 24/7 amigável. | Menor ecossistema de APIs/Terraform que Hetzner; renovações podem ter preços diferenciados. |
| **Contabo** *(Densidade de RAM)* | Cloud VPS S | 4 vCPU, 8 GB RAM, 50 GB NVMe ou 200 GB SSD | ~US$ 6 a US$ 8/mês | Máxima quantidade de RAM/disco por dólar. | Desempenho de I/O de disco mais oscilante, painel de controle mais antigo. |
| **DigitalOcean** *(Managed)* | Basic Droplet | 2 vCPU, 4 GB RAM, 80 GB SSD | ~US$ 24/mês | Ecossistema maduro, excelente documentação e bancos gerenciados. | Custo 2x a 3x maior pelo mesmo hardware de Hetzner/Hostinger. |

> **Recomendação**:
> - Se o objetivo for a **melhor engenharia de longo prazo**: **Hetzner Cloud CPX21** (ou CPX31).
> - Se o objetivo for **agilidade imediata sem criar novas contas de faturamento**: **Hostinger KVM VPS** (já integrado ao seu login).

---

### 2.2 Comparativo de Orquestradores PaaS (Dokploy vs Coolify vs Docker Puro)

Para gerenciar múltiplos projetos em paralelo com banco de dados isolado, subdomínio automático e deploy contínuo via Git, usar Docker puro na mão com Nginx e Certbot gera atrito diário. É aqui que entram os novos PaaS self-hosted modernos:

| Critério | Dokploy (Recomendado) | Coolify | Docker Compose Puro |
| :--- | :--- | :--- | :--- |
| **Consumo de RAM em Idle** | **Leve (~350–500 MB)** | Moderado (~600–800 MB) | Mínimo (~150 MB) |
| **Orquestração Multi-Host** | Suporte nativo a Docker Swarm | Focado em single-server (Swarm experimental) | Manual |
| **Deploy via Git (Webhooks)** | Automático (GitHub / GitLab) | Automático | Requer scripts ou GitHub Actions |
| **Gerenciamento de Bancos** | 1 clique (Postgres, Redis, Mongo, MySQL) | +280 templates de serviços | Requer compose manual |
| **Backups Automáticos** | Nativos para S3/R2/Google Drive | Nativos para S3/R2 | Requer cron jobs com shell script |
| **Licença** | Open-source (com tiers avançados) | Open-source (Apache 2.0 integral) | Open Source |

> **Recomendação**: **Dokploy** é ideal para começar em uma VPS de 4 GB a 8 GB de RAM por ser ultraleve, rápido e nativo para Docker Swarm quando for necessário escalar horizontalmente. **Coolify** é a excelente segunda opção caso queira uma biblioteca maior de templates prontos.

---

### 2.3 Estratégia de Banco de Dados de Baixo Custo & Alta Resiliência

1. **Bancos dos Projetos (PostgreSQL Containerizado na VPS via Dokploy)**:
   - Cada projeto recebe seu banco PostgreSQL ou schema dedicado rodando sobre o SSD NVMe da VPS.
   - **Custo adicional: US$ 0.00**.
2. **Backups Automáticos Off-Site 3-2-1**:
   - **Backup 1 (Local NVMe)**: Snapshots diários na própria VPS.
   - **Backup 2 (Cloudflare R2)**: Dumps compactados e criptografados enviados via S3 API para o Cloudflare R2 (10 GB gratuitos perpétuos, **zero taxa de tráfego de saída/egress**).
   - **Backup 3 (On-Premises 3TB)**: Sincronização noturna via script/rclone para o HDD de 3 TB do servidor On-Premise via rede Tailscale.
3. **Alternativa Serverless para Projetos Específicos**:
   - Para projetos em fase embrionária ou que precisem de branch de dados por Pull Request, o **Neon Serverless Postgres** (free tier generoso) ou **Supabase Free** podem ser plugados sem custo algum.

---

### 2.4 Topologia de Rede & Segurança Zero-Trust

A comunicação entre a máquina de desenvolvimento, o servidor On-Premises e a VPS na nuvem não deve expor portas vulneráveis (como SSH 22 ou Postgres 5432) à internet aberta:

```mermaid
flowchart TD
    subgraph Internet_Publica["Internet Pública & Clientes"]
        UserBrowser["Clientes & Usuários Web"]
        WebhookOrigin["Webhooks Externos & n8n"]
    end

    subgraph Cloudflare_Edge["Cloudflare Edge (Zero Trust)"]
        CF_DNS["Cloudflare DNS & WAF"]
        CF_Tunnel["Cloudflare Tunnel (cloudflared)"]
        CF_Access["Cloudflare Access (Google OAuth)"]
    end

    subgraph Tailscale_Mesh["Tailscale Mesh VPN (Rede Privada Criptografada)"]
        TS_IPs["Sub-rede Segura 100.x.y.z"]
    end

    subgraph Cloud_VPS["Cloud VPS (Hetzner / Hostinger)"]
        Dokploy["Dokploy / Coolify PaaS"]
        App1["Projeto 1 (Next.js / FastAPI)"]
        App2["Projeto 2 (Headless Core)"]
        DB_Prod["PostgreSQL (NVMe Storage)"]
        Backup_Cron["Backup Worker (R2 Sync)"]
    end

    subgraph OnPrem_Server["Servidor On-Premises (Z97 / i7-4790K)"]
        LocalCI["Headless CI & Test Runner"]
        LocalVault["Backup Vault (3 TB HDD + 1 TB Ext)"]
        LocalWorker["Background Task Worker"]
    end

    subgraph Dev_Workstation["Notebook Predator Helios Neo 16"]
        DevIDE["Antigravity IDE & DarkFac"]
        OllamaLocal["Ollama Local (RTX 4070 Ada 8GB)"]
    end

    UserBrowser --> CF_DNS
    WebhookOrigin --> CF_DNS
    CF_DNS --> CF_Tunnel
    CF_Tunnel --> Dokploy
    Dokploy --> App1
    Dokploy --> App2
    App1 --> DB_Prod
    App2 --> DB_Prod

    %% Conexões Privadas via Tailscale
    Dev_Workstation <-->|SSH Seguro / Sync / Dev| TS_IPs
    OnPrem_Server <-->|CI Triggers / Local Backup| TS_IPs
    Cloud_VPS <-->|Replicação / Gestão Interna| TS_IPs

    %% Backups
    Backup_Cron -->|Off-Site Dump (Zero Egress)| Cloudflare_Edge
    Backup_Cron -->|Sync Local Noturno| LocalVault
```

---

## 3. Resumo Financeiro Estimado da Infraestrutura Integrada

| Item | Componente | Custo Atual | Custo Sugerido |
| :--- | :--- | :--- | :--- |
| **Dev Core** | Predator Helios Neo 16 (32GB, RTX 4070) | Já adquirido | US$ 0.00 |
| **On-Premises** | i7-4790K, 16GB, 3TB HDD, 2x 980 Ti, 1200W | Já adquirido | US$ 0.00 |
| **Rede Segura** | Cloudflare Tunnels + DNS + Tailscale | Gratuito | US$ 0.00 |
| **Cloud VPS** | Hetzner CPX21 / Hostinger KVM 2 | Não contratada | ~US$ 9.00 a 14.00/mês |
| **Orquestrador** | Dokploy / Coolify PaaS | - | US$ 0.00 (Self-hosted) |
| **Banco de Dados** | PostgreSQL NVMe + Cloudflare R2 Backups | - | US$ 0.00 (Incluso na VPS) |
| **Hospedagem Web** | Hostinger Sites Simples | Atual (~US$ 4/mês) | US$ 4.00/mês |
| **TOTAL** | **Ambiente Multi-Projeto Completo & Blindado** | **~US$ 4.00/mês** | **~US$ 13.00 a 18.00/mês** |

> Com um investimento de apenas **~US$ 10 a 15 adicionais por mês**, a operação ganha capacidade para sustentar de 5 a 15 projetos web em paralelo com banco de dados isolado, deploys automáticos, backups em 3 camadas e zero portas expostas na internet!
