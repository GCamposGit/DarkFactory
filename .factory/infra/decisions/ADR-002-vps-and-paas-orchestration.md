# ADR-002: Seleção de Provedor de VPS e Orquestrador PaaS (Dokploy vs Coolify)

- **Status**: ACCEPTED
- **Data**: 2026-09-07
- **Decisores**: Operador & Dark Factory Architecture Core

## Contexto

Para operar múltiplos projetos em paralelo na VPS com isolamento de processos, deploys automáticos via Git, roteamento de subdomínios e renovação de certificados SSL, configurar arquivos manuais do Nginx e Docker Compose para cada novo projeto introduz atrito e risco operacional desnecessário.

## Decisão

1. **Provedor de VPS**:
   - **Recomendação Principal**: **Hetzner Cloud** (plano **CPX21** ou **CPX31** na região Ashburn/EUA com 3 a 4 vCPUs AMD EPYC, 4 a 8 GB de RAM e 80 a 160 GB de NVMe, custo ~€8 a €14/mês).
   - **Alternativa Imediata**: **Hostinger KVM 2/4** caso se deseje manter o faturamento unificado na conta Hostinger já existente.
2. **Orquestrador de Containers (PaaS)**:
   - Adotar **Dokploy** (ou **Coolify**) instalado diretamente sobre o Docker Engine da VPS.
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
