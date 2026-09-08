# ADR-003: Estratégia de Banco de Dados Multi-Tenant e Backups Automatizados Off-Site

- **Status**: ACCEPTED
- **Data**: 2026-09-07
- **Decisores**: Operador & Dark Factory Architecture Core

## Contexto

Múltiplos projetos em produção exigem bancos de dados SQL confiáveis, rápidos e isolados. Contratar instâncias gerenciadas (ex: AWS RDS ou DigitalOcean Managed Databases) custaria de US$ 15 a US$ 30 por mês por banco de dados, inviabilizando a meta de excelente custo-benefício.

## Decisão

1. **Instância Central de Banco de Dados de Produção**:
   - Provisionar uma instância de **PostgreSQL 16+** conteinerizada via Dokploy na VPS, alocada sobre o volume SSD NVMe local.
   - Cada projeto possui um banco de dados e usuário isolados com permissões restritas.
   - Custo mensal adicional: **US$ 0.00** (aproveita a memória e disco da VPS).
2. **Estratégia de Backups 3-Camadas**:
   - **Camada 1 (Local)**: Dumps diários gerados automaticamente pela rotina do Dokploy / cron.
   - **Camada 2 (Cloudflare R2)**: Upload criptografado dos dumps para um bucket do **Cloudflare R2** (10 GB perpétuos gratuitos, **taxa zero de transferência de dados/egress**).
   - **Camada 3 (On-Premises Vault)**: Sincronização noturna dos dumps da VPS para o HDD de 3 TB do servidor On-Premise via rede privada Tailscale.
3. **Casos Especiais**:
   - Projetos experimentais ou com demanda de "Database Branching" (uma cópia do banco para cada PR) podem utilizar o plano gratuito do **Neon Postgres** ou **Supabase**.

## Consequências

- **Positivas**:
  - Custo fixo previsível e virtualmente nulo para gerenciar dezenas de bancos de dados.
  - Zero risco de perda de dados: mesmo que a VPS seja destruída, os dados estão intactos no Cloudflare R2 e no disco físico de 3 TB on-premise.
  - Baixíssima latência de leitura e escrita (acesso local ao NVMe na VPS).
- **Negativas / Riscos**:
  - Exige monitorar o espaço livre do disco NVMe da VPS através de alertas automáticos.

## Alternativas Consideradas

- *Managed Cloud Databases*: Rejeitado por custo cumulativo excessivo.
- *SQLite nos containers dos projetos*: Rejeitado por dificuldades em concorrência de escritas e backup consistente em tempo real.
