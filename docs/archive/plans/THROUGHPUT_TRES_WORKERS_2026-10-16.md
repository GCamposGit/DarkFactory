# Três demandas independentes na frota — USR-277

Data: 16/10/2026. Entrega deste ticket: planejamento e fila de implementação. A ativação operacional não faz parte deste pedido.

## Decisões do owner

Fonte: respostas explícitas ao Grill nesta conversa, em 16/10/2026; OA-026, OA-027 e OA-028.

- Uma demanda completa por máquina, em paralelo e de forma independente. Ordem de preferência para máquinas disponíveis: **Grokbox → Notebook → Desktop**.
- A vaga permanece ocupada durante testes e demais etapas do ciclo. Até três ciclos executando; subagentes internos continuam permitidos conforme as regras existentes.
- O workflow recolhe tickets abertos de todos os projetos e repõe a vaga quando o ciclo termina, até não haver trabalho executável. O serviço continua observando novas entradas.
- A seleção de harness permanece em `core.line.routing.pick`. Máquina e harness são decisões distintas.
- O owner informou que o V0 já foi concluído e autorizou esta evolução. O plano pode avançar após o V0; esta declaração não é uma nova medição de CI, deploy ou backup.

## Baseline verificada

SHA de referência: `d465e2e4377e47cf5eb1ac3ac14aa20fc8b5df02`. Checkout compartilhado inicialmente limpo. Não foram medidos tempos atuais dos workers nesta sessão.

| Caminho auditado | Comportamento observado | Ajuste necessário |
|---|---|---|
| `core/line/owner_intake.py` | Ponte idempotente de ticket para run; projetos permitidos por configuração, padrão apenas `darkfac` | Alimentação contínua e cobertura de todos os projetos registrados e aptos |
| `core/orchestrator/adapters/control_postgres.py` | Claim transacional por etapa, com lease e fencing; seleção por `created_at ASC` | Reserva durável da máquina para o ciclo completo |
| `core/orchestrator/cloud_worker.py` | Poll executa uma etapa e disputa a próxima; prioridade por atraso mínimo do job | Afinidade da demanda e reposição por disponibilidade real |
| `core/orchestrator/cloud_coordinator.py` | Supervisão/outbox; `max_slots` configurável, padrão 2, exposto no status | Admissão efetiva de até três ciclos, com capacidade por host |
| `core/line/line_lock.py` e `run_ticket.py` | Exclusão de lançamentos concorrentes no checkout da mesma máquina | Unificar admissão manual e contínua sem remover a proteção local |
| `core/line/workspace.py` | Worktree por projeto/run e continuidade por branch | Reutilizar isolamento existente e preservar candidato imutável |
| `core/harness/remote_dispatch.py` | Padrão Grokbox → Desktop; worker ocupado pode ser esperado por 300 s | No ciclo distribuído, executar a validação no próprio host atribuído |
| `scripts/start_onprem_worker.ps1` | Desktop como secondary, Notebook como fallback; padrão 3 slots | Configuração explícita da nova topologia: 1 ciclo por host |
| `core/projects/models.py`, `core/line/stage_build.py` | Comandos e restrições por projeto; desenvolvimento e validação separados | Cada projeto usa seus próprios comandos e capacidades |
| `core/orchestrator/guard.py` | Governança e `core/harness/*` protegidos | Usar interfaces existentes; nenhuma alteração protegida é necessária ao desenho inicial |

As diferenças acima são lacunas em relação ao novo requisito, não alegações de regressão do comportamento anterior. Elas serão cobertas pelas fatias da fila abaixo.

## Contrato de operação

### Admissão e escolha de máquina

1. Conciliar ledger e runs existentes, sem duplicar ticket já submetido. Identidade: projeto + ticket; tentativas preservam o protocolo de idempotência existente.
2. Considerar somente trabalho executável: projeto registrado, especificação pronta para a etapa, dependências satisfeitas e acesso/capacidades suficientes. Concluídos e cancelados ficam fora. Pendências humanas, orçamento e dependências aparecem com motivo e não bloqueiam a procura pelo próximo ticket apto.
   Tickets adiados exclusivamente pelo congelamento V0 são reavaliados após a declaração de encerramento do owner: a tag histórica não deve excluí-los silenciosamente. Adiamentos intencionais por negócio ou data permanecem explícitos; todo ticket aberto precisa aparecer como apto ou com um bloqueio verificável.
3. Preservar a ordem atual da fila, do pronto mais antigo ao mais novo, com desempate estável por projeto e ticket. Esta mudança não cria uma nova prioridade comercial entre projetos.
4. Para cada ticket, escolher o primeiro host saudável, livre e compatível na ordem Grokbox, Notebook, Desktop. Prioridade só desempata capacidade disponível; não espera uma máquina ocupada e não interrompe um ciclo já iniciado.
5. Reservar ticket e host atomicamente no controle compartilhado. No máximo um ciclo por host e três na frota, inclusive diante de dois coordenadores ou lançamentos CLI simultâneos.
6. Publicar heartbeat, disponibilidade, capacidades e motivo de indisponibilidade. Expiração é configurável e baseada no mecanismo de leases existente; não inventar tempos operacionais sem verificar o contrato atual.

Com três tickets aptos e três hosts compatíveis livres, os três iniciam. Com um ticket e três hosts livres, ele vai para o Grokbox. Com o Grokbox ocupado, o próximo inicia no Notebook imediatamente; em seguida, no Desktop.

### Ciclo independente por host

- O host reservado executa o workflow existente até seu desfecho: preparação, desenvolvimento, testes, revisão, integração e release aplicável. A reserva não é liberada ao iniciar testes, ao aguardar CI ou entre etapas.
- A escolha de harness ocorre por estágio, usando as capacidades autenticadas daquele host e o roteador atual: cota acima de 15%, cooldown, modo write, preferência de operação quando aplicável e revisão em outra família continuam vigentes.
- Workers em background não recebem uma preferência interativa artificial por herdarem variáveis da sessão que os iniciou. Não fixar Grok no Grokbox, nem Codex no Notebook.
- Subagentes trabalham dentro da demanda e de seu orçamento; não criam novas vagas de demanda nem uma segunda suíte completa na máquina.
- Worktree, ambiente, artefatos e evidências são isolados por projeto/run. Não compartilhar branch de desenvolvimento ou atribuir um PASS a outro SHA/configuração.
- No caminho gerenciado, testes rodam no host atribuído. Para DarkFac, usar o portão oficial com execução local via interface já disponível; não encaminhar as três suítes ao mesmo worker remoto. Projetos externos usam `ProjectCommands` resolvidos para seu próprio repositório.
- O bloqueio de suíte por máquina permanece. Validação de outro cliente/CLI precisa respeitar a mesma capacidade física e ser visível ao agendador.

### Reposição e recuperação

- Ao concluir, falhar definitivamente ou cancelar um ciclo, persistir o resultado e liberar a reserva uma única vez. Buscar imediatamente o próximo ticket apto; se a fila estiver vazia, continuar observando novas entradas.
- Falha recuperável segue os limites existentes de retry. Falha esgotada não é reenfileirada infinitamente no mesmo tick; conserva causa e evidência para correção formal.
- Uma demanda que passa a depender exclusivamente de intervenção humana é estacionada em estado explícito, com contexto salvo e sem processo executando. A retomada passa pela admissão e compete por uma das três vagas; nunca executa como um quarto ciclo. Isso permite usar a máquina que ficou ociosa.
- Host offline não recebe trabalho novo. Antes de reassumir seu ciclo em outro host, expirar/revogar a reserva anterior, confirmar fencing e reconciliar efeitos Git/deploy já iniciados. O processo antigo não pode publicar após perder a posse.
- Perda do banco interrompe novas admissões; não cria uma fila local concorrente. Efeitos com resultado desconhecido são conciliados antes de repetir.
- Distinguir `fila executável vazia` de `há tickets bloqueados`. A fábrica pode ficar sem trabalho apto sem declarar todos os tickets entregues.

### Integração e atualização dos ambientes

- Desenvolvimentos e suítes podem rodar simultaneamente em repositórios iguais, cada qual na sua worktree.
- Integração usa exclusão compartilhada por repositório/branch de destino. Dentro da seção protegida: sincronizar base, resolver conflitos, validar o candidato exigido e mergear com CI verde. Base alterada invalida evidência que não corresponde ao novo candidato.
- Deploy usa exclusão por destino, permitindo que projetos independentes prossigam. O ciclo só é entregue após a evidência de release/smoke exigida pelo projeto.
- Atualizações da frota devem drenar cada host e sincronizá-lo quando estiver seguro, sem reiniciar os outros ciclos ativos. Reutilizar autonomia Git, deploy e backup existentes.

## Desenho técnico

Reutilizar o coordenador e o Postgres da linha. A fila é uma projeção dos tickets e estados persistidos, e não um novo backlog em arquivo. Acrescentar política pura de seleção e reservas duráveis de ciclo/host; os claims de etapa permanecem subordinados à reserva do ciclo.

Contrato mínimo da reserva: projeto, ticket, run, host, estado, época/fencing, início, heartbeat e expiração. A disponibilidade de host precisa refletir execução local, drain e capacidade de teste. Unicidade de host ocupado e de ticket ativo deve ser imposta em transação, não apenas por contagem em memória.

Adaptadores de produção e de testes devem apresentar o mesmo comportamento. Migração é aditiva; runs antigos são conciliados antes de ativar o modo de três hosts. Um rollout desabilitado preserva a semântica anterior até a habilitação controlada.

Não é suficiente trocar `max_slots` para 3: o limite atual se refere a etapas e não garante afinidade, admissão entre processos ou um ciclo por host. Também não se deve aplicar uma trava de integração global a todos os projetos.

## Fatias executáveis

Cada fatia terá ticket próprio, até três critérios e até quatro arquivos principais; os testes novos serão explicitados no ticket. A ordem abaixo indica dependência, não autorização para iniciar implementação neste pedido.

| Fatia | Entrega | Dependências | Superfície principal |
|---|---|---|---|
| F1 / USR-280 | Política pura de admissão e topologia de três hosts | USR-277 | novo `core/line/fleet_policy.py`, nova `.factory/config/line_fleet.json`, testes da política |
| F2 / USR-281 | Reservas transacionais, inventário de hosts e fencing por ciclo | F1 | `core/workflow/control_contracts.py`, `core/workflow/control_store.py`, `core/orchestrator/adapters/control_postgres.py`, testes de concorrência |
| F3 / USR-282 | Worker com afinidade de ciclo e coordenador com reposição | F2 | `core/orchestrator/cloud_worker.py`, `core/orchestrator/cloud_coordinator.py`, testes de loop e recuperação |
| F4 / USR-283 | Alimentador contínuo de tickets de todos os projetos aptos | F1, F2 | novo `core/line/backlog_feeder.py`, `core/line/owner_intake.py`, testes de intake/alimentador |
| F5 / USR-284 | Admissão única para CLI e validação no host reservado | F2, F3 | `run_ticket.py`, `core/line/line_lock.py`, `core/line/stage_build.py`, testes do fluxo gerenciado |
| F6 / USR-285 | Integração e deploy coordenados por repositório/destino | F2, F3 | novo `core/line/resource_lease.py`, `core/line/stage_integration.py`, `core/line/stage_release.py`, testes de exclusão |
| F7 / USR-286 | Configuração e ativação dos três hosts com drain | F3, F4, F5, F6 | `scripts/start_onprem_worker.ps1`, novo `scripts/start_fleet_worker.py`, novo runbook da frota, testes de bootstrap |
| F8 / USR-287 | Evidência de capacidade, motivos de ociosidade e ensaio fim a fim | F7 | `core/workflow/line_live.py`, `hub/frontend/live.js`, testes de projeção/piloto e relatório do piloto |

F8 aproveita a projeção existente `LineLiveSnapshot` (`core/workflow/line_live.py`), já exposta por `hub/backend/api.py` em `/api/line/live` e lida por `hub/backend/service.py`. A camada de domínio mantém a leitura independente do frontend. As rotas existentes são somente leitura neste desenho.

## Validação e critério de saída

1. Testes determinísticos: seis tickets aptos, de pelo menos dois projetos, produzem três ciclos em hosts distintos; duas finalizações simultâneas repõem duas vagas, sem duplicação nem quarto ciclo. Verificar ordem de host quando há capacidade disponível, salto de ticket incompatível e permanência da reserva durante testes/CI.
2. Exercitar com Postgres real: disputa entre coordenadores e CLI, expiração de lease, reentrada do worker antigo e efeitos idempotentes. Mock sozinho não comprova exclusão distribuída. Cobrir também falta de harness, cota crítica, projeto externo com comandos próprios e projeto bloqueado sem impedir os demais.
3. Ensaio operacional: observar os três hosts executando seus candidatos, registrar reposição, falha de um host e retomada sem duplicação; verificar uma suíte completa por host e integração segura no mesmo repositório. Medir entregas/hora, ocupação por host, espera em testes e tempo de reposição. Não prometer ganho de 3x sem medição.

Cada implementação usa testes focados primeiro e, ao final, o portão oficial `python C:\dev\DarkFac\core\harness\runner.py --quick` na worktree do candidato. Verificação manual de SHA commitado usa `python C:\dev\DarkFac\scripts\gate_isolated.py --ref <SHA>`. Não executar uma segunda suíte completa separada.

Ativação: inventariar acessos e CLIs por host, validar migração, instalar/atualizar o serviço autônomo com um ciclo por host e executar o piloto. Parâmetros iniciais: nomes lógicos grokbox/notebook/desktop, prioridades 1/2/3 e capacidade 1/1/1. Endereços, caminhos e referências de credenciais são descobertos da configuração de cada host, nunca inventados nem copiados como segredos para o repositório.

Rollback: suspender novas admissões, drenar ciclos, preservar branches/evidências e voltar ao modo anterior por configuração. Não apagar reservas ativas, não matar suítes saudáveis e não resetar worktrees.

## Ações humanas e governança

As três decisões de negócio deste plano foram respondidas pelo owner e devem constar como resolvidas no backlog de ações. Nenhuma configuração manual é exigida neste planejamento. Se o inventário da implementação revelar acesso exclusivo do owner, registrar uma OA com passos completos, verificação e tickets bloqueados no mesmo turno; não pedir que o owner sincronize Git, faça deploy ou opere backups.

A declaração de encerramento do V0 resolve a autorização desta demanda. Atualizações de documentos protegidos continuam sujeitas ao contrato de governança; este plano não altera `AGENTS.md`, `FACTORY_RULES.md`, o portão ou os modelos do roteador.
