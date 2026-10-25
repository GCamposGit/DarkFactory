# GRILL

## Premissas
- O control store Postgres (core/workflow/control_store.py + adapter Postgres) e a logica de merge-sobre-arquivo em core/demands/queue_store.py e core/owner_actions/control_queue.py ja evitam PRs de registro (USR-343 S4a/S4b); este ticket so precisa agendar scripts/snapshot_queue.py para rodar diariamente (reaproveitando o padrao de daemon de core/infra/backup_cron.py), nao recriar a logica de fila.
- Nao sera trocado o motor de banco: se DARKFAC_HF02_DATABASE_URL nao estiver configurada para Postgres real em algum ambiente, isso e tratado como acao do owner (cadastrar a variavel), nao como mudanca de codigo.
- A autorizacao Ed25519 em core/governance/authorization.py ja rejeita a string 'owner' sem assinatura valida; nenhuma mudanca de modelo de assinatura e necessaria, so testes/verificacao de cobertura.
- O cofre SOPS+age (ADR-cofre-sops-age.md, core/infra/secrets_vault.py, scripts/bootstrap_secrets.py) e o cofre 'escolhido por ADR' citado na demanda -- nao sera criada uma nova ADR nem trocada a tecnologia de cofre.
- core/line/agent_env.py ja nega padroes DOKPLOY_, TELEGRAM_, GITHUB_TOKEN/GH_TOKEN, SOPS_, AGE_, GH_APP_ com testes (test_agent_env_usr370.py); o trabalho aqui e fechar lacunas remanescentes (ex.: leitura direta de .env fora do agent_env), nao construir o isolamento do zero.
- O conceito de 'broker' separado para push/merge/deploy ja esta descrito em core/governance/publication.py; este ticket verifica/completa essa wiring em vez de inventar o conceito.
- O fluxo de aceite comercial (requires_commercial_acceptance) permanece intocado, conforme fora de escopo explicito na demanda.
- (technical) Para a aprovacao de um toque via Telegram em diffs de MISSION.md/FACTORY_RULES.md/AGENTS.md, qual bot/chat deve ser autorizado? core/integrations/telegram.py ja tem bot, authorized_chat_ids e callback /approve funcionando, mas hoje so para deploy e grill. -> **Reaproveitar o bot e chat do owner ja autorizados para deploy (TELEGRAM_OWNER_BOT_TOKEN)** (Evita gerenciar outro token no cofre e reusa authorized_chat_ids e o callback /approve ja testados.)
- (technical) Regra de 'acao repetida da mesma categoria gera ticket de automacao': o que conta como repeticao? -> **Mesma categoria 2x no total, sem expirar** (E a regra mais simples de implementar e auditar sobre a tabela owner_actions existente no control store; uma janela movel exige um prazo arbitrario que so o owner pode validar.)

## Decisoes do owner
- (nenhuma pergunta bloqueante)

## Aguardando decisao do owner
- [business] Escopo exato de guard.py apos a mudanca: hoje ele protege AGENTS.md, MISSION.md, FACTORY_RULES.md, core/governance/*, .factory/governance/owner_pubkey e .github/workflows/* -- e NAO protege core/harness/* (ao contrario do que a secao Problema da demanda afirma). A demanda pede que guard proteja so os 3 arquivos .md. Isso deve remover a protecao de core/governance/*, owner_pubkey e .github/workflows/*? (id=q1)
- [intent] Boa parte da jornada descrita ja existe no codigo (fila sem PR via control store Postgres em core/workflow/control_store.py e core/demands/queue_store.py, GitHub App token em core/git/app_token.py, assinatura Ed25519 em core/governance/authorization.py, cofre SOPS+age em docs/adr/ADR-cofre-sops-age.md e core/infra/secrets_vault.py, isolamento de env em core/line/agent_env.py com testes). Como tratar isso? (id=q2)
- [account] core/git/app_token.py ja usa uma GitHub App quando GH_APP_ID/GH_APP_INSTALLATION_ID/GH_APP_PRIVATE_KEY estao configurados, mas cai de volta para a sessao gh pessoal do owner se nao estiverem. Esse App ja existe e esta instalado neste repositorio? (id=q3)
