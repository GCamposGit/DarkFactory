# USR-370: allowlist de rede do agente (decisao de inviabilidade)

Status: registrado como inviavel sem privilegios de administrador (2026-10-24).

Mecanismos avaliados para restringir o agente (claude/codex/grok) aos hosts de
`.factory/config/agent_env_policy.json` (`network_allowlist.hosts`):

- Proxy via `HTTP(S)_PROXY`: e apenas convencao. CLIs de agente, `git`, `curl` e qualquer
  subprocesso podem ignora-la ou abrir sockets diretos; nao e controle de seguranca.
- Firewall do Windows (`netsh advfirewall`) ou `iptables`/namespaces no Linux: exigem
  administrador/root, que os workers (Notebook, Desktop, grokbox) nao devem ter para o agente.
- Resolucao por IP: hosts de API usam CDN com IPs rotativos; regras por IP quebram sem aviso.

Decisao: o isolamento efetivo desta fatia e o de credenciais (`build_agent_env`, capabilities
por ticket). A allowlist permanece documentada como insumo para um sandbox futuro (container
com rede restrita no grokbox). Reabrir quando houver runner em container.

Tambem decidido: o `child_env` do `run_ticket.py` (subprocesso `--resume-delivery`) NAO usa
`build_agent_env`, pois e o executor de entrega e precisa das credenciais de git/deploy.
