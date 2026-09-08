# ADR-001: Topologia Híbrida Multi-Projeto (Dev Core + On-Premise + Cloud VPS + Edge)

- **Status**: ACCEPTED
- **Data**: 2026-09-07
- **Decisores**: Operador & Dark Factory Architecture Core

## Contexto

A Dark Factory precisa evoluir da gestão de 1 projeto simples de cada vez para a gestão de múltiplos projetos em paralelo com alta segurança, estabilidade e excelente custo-benefício.

O operador dispõe de:
1. Notebook Predator Helios Neo 16 (i7 moderno, 32GB RAM, RTX 4070 Ada Lovelace 8GB).
2. Servidor On-Premises Z97 (i7-4790K 4.0GHz, 16GB DDR3, 240GB SSD + 3TB HDD + 1TB Ext, 2x GTX 980 Ti Maxwell, Fonte 1200W).
3. Serviços e contas em nuvem: Hostinger, n8n, Cloudflare (DNS/email), Google.

## Decisão

Adotar uma **Topologia Híbrida em 3 Camadas Especializadas**:

1. **Camada 1: Dev Core & Local AI Inference (Predator Helios Neo 16)**
   - Papel: Desenvolvimento ágil e inferência de IA local com baixa latência usando a RTX 4070 (Ada Lovelace, 8GB VRAM) com modelos quantizados no Ollama e transcrição de áudio em FP16.
2. **Camada 2: Headless Storage & Staging Worker (Servidor On-Premises Z97)**
   - Papel: Cold storage local de alta capacidade (3TB Hitachi + 1TB Samsung P3), cofre de backups duráveis e runner de testes pesados/CI sem ocupar recursos do notebook de trabalho.
   - Restrição Técnica: As GPUs GTX 980 Ti (Maxwell, CC 5.2) não serão utilizadas para inferência de LLMs modernos para evitar consumo elétrico excessivo (~500W) e suporte descontinuado a tensores modernos em float16.
3. **Camada 3: Cloud Production & Multi-Project Hub (Cloud VPS + Cloudflare Edge)**
   - Papel: Hospedagem 24/7 de aplicações web, APIs voltadas a clientes, bancos de dados em produção e automações n8n, com conectividade gigabit de datacenter e SLA de 99.9%.

## Consequências

- **Positivas**:
  - Aproveitamento inteligente e de custo zero do hardware on-premise existente sem distorcer seu papel.
  - Disponibilidade ininterrupta dos serviços online sem depender de link de internet residencial ou nobreak doméstico.
  - Segurança em camadas (dados sensíveis e backups replicados localmente e na nuvem).
- **Negativas / Riscos**:
  - Exige manter a rede segura unificada entre os três nós (resolvido pelo Tailscale e Cloudflare Tunnels).

## Alternativas Consideradas

- *Hospedar tudo localmente no On-Premise*: Rejeitado devido a limitações de upload residencial, IP dinâmico e risco de interrupção elétrica.
- *Hospedar tudo em hiperescaladores (AWS/GCP)*: Rejeitado devido a custos elevados e imprevisíveis para múltiplos projetos em estágio inicial.
