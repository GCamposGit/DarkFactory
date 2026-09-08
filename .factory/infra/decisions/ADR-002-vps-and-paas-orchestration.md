# ADR-002: Seleção de Provedor de VPS e Orquestrador PaaS (Dokploy vs Coolify)

- **Status**: ACCEPTED
- **Data**: 2026-09-07
- **Decisores**: Operador & Dark Factory Architecture Core

## Atualização oficial — 08/09/2026

O owner ratificou a VPS na **Alemanha**. Inventário atual: Hetzner **CX23**, Falkenstein, 2 vCPU / 4 GB / 40 GB. A proposta Ashburn/CPX21 abaixo foi superada e não deve orientar novo provisionamento. Dokploy e PostgreSQL existentes são reaproveitados. O owner esclareceu que a instalação Ubuntu/formatação foi abandonada, mantendo Windows com Linux via WSL2 no servidor preservado. INFRA-03 associa esse setup ao nó local. O owner confirmou a VPS com configuração própria, padrão da Hetzner, separada do Windows/WSL2 local. A imagem/versão da VPS deve ser verificada por leitura técnica antes de scripts específicos; o guia histórico não comprova Ubuntu instalado. Ver [requisitos do workflow](../../../docs/HYBRID_AUTONOMY_REQUIREMENTS.md).

## Contexto

Para operar múltiplos projetos em paralelo na VPS com isolamento de processos, deploys automáticos via Git, roteamento de subdomínios e renovação de certificados SSL, configurar arquivos manuais do Nginx e Docker Compose para cada novo projeto introduz atrito e risco operacional desnecessário.

## Decisão

1. **Provedor de VPS: Hetzner Cloud (Decisão Definitiva Aprovada)**:
   - Contratação de instância na **Hetzner Cloud** na região de **Falkenstein, Alemanha**, conforme decisão oficial de 08/09/2026. Ashburn foi a proposta histórica, agora superada.
   - Plano vigente no inventário: **CX23**, 2 vCPUs, 4 GB RAM, 40 GB NVMe. Preço e capacidade de expansão serão conferidos antes de contratação; CPX21/CPX31 eram hipóteses históricas.
   - Faturamento pós-pago por hora sem fidelidade contratual e com hardware enterprise de altíssimo desempenho de I/O de disco.
2. **Orquestrador de Containers (PaaS): Dokploy**:
   - Adotar **Dokploy** instalado diretamente sobre o Docker Engine da VPS.
   - Dokploy é escolhido como recomendação primária para servidores de 4GB a 8GB de RAM devido ao seu consumo de memória ultrabaixo em repouso (~350MB vs ~700MB do Coolify) e suporte nativo a Docker Swarm para escala horizontal futura.

## Consequências

- **Positivas**:
  - Provisionamento de um novo projeto web (Next.js, FastAPI, Node, etc.) e seu respectivo subdomínio com HTTPS em menos de 2 minutos.
  - Deploy contínuo ativado automaticamente a cada `git push` no GitHub.
  - Painel unificado para monitorar consumo de CPU, RAM e logs de containers de todos os projetos simultaneamente.
- **Negativas / Riscos**:
  - Responsabilidade do operador pela segurança do sistema operacional da VPS (a identificar por preflight técnico, sem presumir Ubuntu instalado) (blindada via Tailscale e UFW).

## Alternativas Consideradas

- *Docker Compose Puro + Nginx Manual + Certbot*: Rejeitado por exigir manutenção repetitiva e propensa a erros manuais a cada novo projeto.
- *Kubernetes / k3s*: Rejeitado por overhead excessivo de memória e complexidade desnecessária para esta fase da fábrica.
