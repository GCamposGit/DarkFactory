# ADR-004: Rede Blindada Zero-Trust com Cloudflare Tunnels e Tailscale Mesh

- **Status**: ACCEPTED
- **Data**: 2026-09-07
- **Decisores**: Operador & Dark Factory Architecture Core

## Contexto

A infraestrutura híbrida conecta um notebook de desenvolvimento, um servidor físico em rede doméstica residencial e uma VPS em datacenter externo. Expor portas administrativas (como SSH na porta 22, PostgreSQL na 5432, ou portas do Docker) para a internet pública atrai bots maliciosos, ataques de força bruta e varreduras automatizadas. Além disso, a internet residencial não possui IP público fixo e bloqueia portas de entrada.

## Decisão

Adotar uma arquitetura de rede **Zero-Trust com portas 100% fechadas para o mundo externo**:

1. **Camada Pública de Aplicação: Cloudflare Tunnels (`cloudflared`)**
   - Os serviços web públicos (sites dos clientes, APIs, webhooks) são expostos exclusivamente via túnel criptografado de saída (`cloudflared`).
   - Nenhum roteador doméstico precisa de port forwarding (abertura de portas no modem/roteador).
   - O tráfego passa pela rede da Cloudflare, com proteção DDoS integrada, SSL gerenciado e WAF.
   - Painéis de administração interna (ex: painel do Dokploy, Portainer, métricas) são protegidos pelo **Cloudflare Access** exigindo autenticação OAuth via conta Google pessoal antes de carregar qualquer página.
2. **Camada Privada de Nós: Tailscale Mesh VPN (WireGuard)**
   - O Notebook Predator, o Servidor On-Premises e a VPS Cloud são membros da mesma rede privada Tailscale.
   - Acesso SSH, sincronização de arquivos de backup e comandos remotos trafegam exclusivamente via IPs privados `100.x.y.z`.
   - O firewall da VPS (`ufw`) bloqueia conexões SSH vindas da internet aberta, permitindo acesso apenas pela interface `tailscale0`.

## Consequências

- **Positivas**:
  - Segurança de nível bancário sem custos de licenciamento.
  - Zero necessidade de lidar com Dynamic DNS (DDNS) ou pagar por IP fixo na operadora residencial.
  - Painéis de controle blindados por autenticação Google em 2 etapas.
- **Negativas / Riscos**:
  - Exige que o daemon do Tailscale esteja rodando nos nós para acesso administrativo.

## Alternativas Consideradas

- *Abertura de portas 80/443/22 no roteador doméstico*: Rejeitado categoricamente por alto risco de invasão e instabilidade de IP.
- *OpenVPN tradicional*: Rejeitado por complexidade de emissão manual de certificados e manutenção de servidor de VPN dedicado.
