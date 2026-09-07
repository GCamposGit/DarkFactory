# Próximos Passos Imediatos: Ativação da Infraestrutura Híbrida Multi-Projeto

Este guia detalha exatamente o que deve ser feito no ambiente para colocar a arquitetura em funcionamento passo a passo, aumentando a produtividade sem gerar complexidade ou custos prematuros.

---

## Passo 1: Instalação e Conexão da Rede Privada Segura (Custo $0, ~15 min)

O primeiro passo para unificar os nós sem complicação de rede, portas de firewall ou VPNs legadas é o **Tailscale**:

1. Crie uma conta gratuita no [Tailscale](https://login.tailscale.com/) usando sua conta Google pessoal.
2. Instale o Tailscale no **Notebook Predator Helios Neo 16** (Windows).
3. Ao subir o servidor On-Premises (Passo 2) e a VPS (Passo 3), basta instalar o cliente Tailscale neles.
4. **Resultado Imediato**:
   - Cada nó ganha um IP fixo privado seguro na faixa `100.x.y.z` (ex: `100.101.10.1` para o notebook, `100.101.10.2` para o servidor, `100.101.10.3` para a VPS).
   - Você poderá acessar SSH, banco de dados ou painéis administrativos de qualquer lugar pelo notebook sem precisar abrir portas para a internet.

---

## Passo 2: Preparação do Servidor On-Premises (Z97 / i7-4790K)

Para transformar a máquina de 2015 em um servidor headless estável e econômico:

1. **Recomendação de Hardware**:
   - As duas GTX 980 Ti consomem muita energia (~500W sob carga, ~100W em idle) e não aceleram os LLMs modernos eficientemente.
   - **Ação sugerida**: Deixar apenas uma GPU instalada (ou usar o vídeo integrado Intel HD 4600 da CPU se a placa-mãe suportar display headless), o que reduz drasticamente o consumo elétrico da fonte de 1200W para menos de ~40W em idle.
2. **Instalação do Sistema Operacional**:
   - Instalar **Ubuntu Server 24.04 LTS (x86_64)** no SSD Kingston de 240 GB.
   - Formatar o HDD Hitachi de 3 TB como partição dedicada de armazenamento de dados montada em `/mnt/storage_vault`.
   - Plugar o Samsung P3 de 1 TB como partição secundária de rotação de backups.
3. **Instalação de Pacotes Básicos**:
   ```bash
   sudo apt update && sudo apt install -y docker.io docker-compose-v2 curl git ufw rclone
   sudo systemctl enable --now docker
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```
4. **Resultado Imediato**:
   - O servidor vira um nó headless acessível via `ssh user@onprem-node` pela rede Tailscale.
   - Serve como repositório de 3 TB de backups e runner de testes pesados.

---

## Passo 3: Contratação e Setup da VPS Integrada

Quando estiver pronto para colocar os projetos online para clientes e testes reais:

1. **Escolha do Provedor**:
   - **Opção A (Recomendada pela Engenharia)**: Abrir conta na [Hetzner Cloud](https://www.hetzner.com/cloud), criar um servidor **CPX21** (ou CPX31) na região de Ashburn/EUA (menor latência para o Brasil) rodando **Ubuntu 24.04 LTS**. Custo: ~€8 a €14/mês.
   - **Opção B (Conveniência)**: Ativar um plano **Hostinger KVM 2** diretamente no painel Hostinger que você já possui.
2. **Instalação do Dokploy (em 1 comando na VPS)**:
   ```bash
   curl -sSL https://dokploy.com/setup.sh | sh
   ```
3. **Acesso ao Painel Dokploy**:
   - Acesse `http://<ip-da-vps>:3000` e crie o usuário administrador master.
4. **Instalação do Cloudflare Tunnel na VPS**:
   - No painel da Cloudflare (Zero Trust > Networks > Tunnels), crie um túnel (ex: `darkfac-vps-tunnel`).
   - Copie o comando de 1 linha gerado pelo painel e rode na VPS.
   - Aponte os subdomínios (ex: `hub.seudominio.com`, `api.seudominio.com`, `dokploy.seudominio.com`) diretamente para os serviços da VPS com SSL automático da Cloudflare.
5. **Resultado Imediato**:
   - Deploy de novos projetos em menos de 2 minutos via Git push.
   - Bancos de dados PostgreSQL isolados criados com 1 clique.
   - Zero necessidade de configurar Nginx reverso ou renovar certificados SSL na mão.

---

## Passo 4: Rotina de Produtividade Diária na Dark Factory

Com essa infraestrutura ativa, seu fluxo de trabalho diário fica extremamente fluido:

1. **Desenvolvimento Local (Notebook Predator)**:
   - Você programa normalmente com o Antigravity IDE.
   - Modelos locais de suporte rodam na sua **RTX 4070** local sem custo de token.
2. **Validação Automática e Integração**:
   - Testes e builds rodam no runner headless da Dark Factory.
   - Se os testes forem muito extensos, podem ser despachados para o On-Premises.
3. **Deploy em Produção / Staging**:
   - Um `git push` para a branch `main` dispara o webhook do Dokploy na VPS.
   - O container da nova aplicação é compilado e colocado no ar instantaneamente sem downtime.
4. **Backups Blindados**:
   - O Dokploy realiza o dump diário do PostgreSQL e envia direto para o Cloudflare R2 (grátis) e para o On-Premises via Tailscale.

---

## Resumo dos Comandos da Dark Factory para Gerenciar a Infraestrutura

Você pode inspecionar e acompanhar a infraestrutura a qualquer momento diretamente pelo terminal da Dark Factory:

```powershell
# Listar todos os nós e seus custos
python C:\dev\DarkFac\core\infra\cli.py list

# Ver o status geral e quantidade de serviços
python C:\dev\DarkFac\core\infra\cli.py status

# Inspecionar especificações técnicas de um nó
python C:\dev\DarkFac\core\infra\cli.py inspect predator-neo-16
python C:\dev\DarkFac\core\infra\cli.py inspect onprem-z97-server
python C:\dev\DarkFac\core\infra\cli.py inspect cloud-vps-primary

# Exportar relatório em Markdown
python C:\dev\DarkFac\core\infra\cli.py export-markdown -o C:\dev\DarkFac\.factory\infra\status_report.md
```
