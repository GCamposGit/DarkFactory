# GRILL

## Premissas
- A demanda contém objetivo, fronteiras e aceite suficientes; não há decisão bloqueante do owner nesta etapa.
- Esta rodada é exclusivamente de leitura e grill; não cria arquivos, tickets nem executa alterações operacionais.
- A implementação deverá reutilizar CloudCoordinator, FeederCycle, BacklogFeeder, ProjectRegistry, roteador e control store existentes; quality_cycle.py só será criado se a inspeção demonstrar necessidade.
- A divergência relatada para USR-449 integra a entrega. Sua causa permanece não confirmada: reconciliar versão implantada, configuração e leitura do store antes de introduzir adaptações; indisponibilidade deve permanecer distinta de ticket inexistente.
- O ciclo será reutilizável por projeto, com demonstração inicial real em darkfac. Cadastro de projeto, isoladamente, não amplia objetivos, acessos ou permissões de remediação.
- Consolidação diária de avisos e telemetria e revisão documental semanal são cadências iniciais configuráveis, complementadas por eventos e vencimento de evidências; não constituem prazos de implantação.
- Persistência, deduplicação, retomada, invalidação de cache e consolidação de rodadas atrasadas são decisões técnicas subordinadas aos critérios comportamentais descritos.
- A qualificação usará evidências verificáveis e políticas existentes; ausência de baseline ou evidência suficiente será registrada como limitação de cobertura, sem inventar limiares de negócio ou resultados.
- O roadmap deverá ser dividido em unidades pequenas pela esteira vigente, respeitando dependências, capacidade elegível, orçamento atual e piso de cota, sem estimativas de prazo.
- A entrega exige validação na worktree imutável do candidato, revisão por família diferente, CI, merge, deploy, smoke e rechecagem real; aceite automático segue o fluxo autorizado.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
