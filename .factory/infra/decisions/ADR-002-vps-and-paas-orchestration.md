# ADR-002: Seleção de Provedor de VPS e Orquestrador PaaS (Dokploy vs Coolify)

- **Status**: ACCEPTED
- **Data**: 2026-09-07
- **Decisores**: Operador & Dark Factory Architecture Core

## Contexto

Para operar múltiplos projetos em paralelo na VPS com isolamento de processos, deploys automáticos via Git, roteamento de subdomínios e renovação de certificados SSL, configurar arquivos manuais do Nginx e Docker Compose para cada novo projeto introduz atrito e risco operacional desnecessário.

## Decisão

1. **Provedor de VPS: Hetzner Cloud (Decisão Definitiva Aprovada)**:
   - Contratação de instância na **Hetzner Cloud** na região de **Ashburn, VA (EUA)** (menor latência para o Brasil, ~110ms).
   - Plano inicial: **CPX21** (3 vCPUs AMD EPYC, 4 GB RAM, 80 GB NVMe, ~€ 7.05 a € 8.50/mês) com possibilidade de upgrade transparente para **CPX31** (4 vCPUs, 8 GB RAM, 160 GB NVMe, ~€ 13.40/mês).
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
  - Responsabilidade do operador pela segurança do sistema operacional Linux da VPS (blindada via Tailscale e UFW).

## Alternativas Consideradas

- *Docker Compose Puro + Nginx Manual + Certbot*: Rejeitado por exigir manutenção repetitiva e propensa a erros manuais a cada novo projeto.
- *Kubernetes / k3s*: Rejeitado por overhead excessivo de memória e complexidade desnecessária para esta fase da fábrica.
