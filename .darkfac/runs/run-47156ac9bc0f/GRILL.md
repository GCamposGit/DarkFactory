# GRILL

## Premissas
- A rodada é exclusivamente de leitura; não foram criados arquivos, tickets nem executadas integrações.
- README, AGENTS.md e código de FeederCycle e BacklogFeeder foram inspecionados; reutilizar a coordenação, o registro de projetos, o roteador e o control store existentes.
- A busca local não encontrou QualityCycle nem implementação do marco Python de análise de vulnerabilidades; reconciliar sua base entregue e seus contratos antes de planejar extensões.
- Todos os grupos citados permanecem no escopo: lockfiles Node, imagens por digest, SBOM e IaC; formatos e versões suportados serão definidos tecnicamente e terão cobertura explícita.
- Para Node, considerar versões resolvidas e dependências transitivas dos lockfiles npm, pnpm e Yarn, respeitando particularidades de cada formato.
- Digest identifica uma imagem imutável, mas não comprova seu conteúdo nem aplicabilidade; associar inventário verificável, plataforma e ambiente à imagem avaliada.
- SBOM será tratado como evidência com proveniência, versão do formato e vínculo ao artefato; componentes ausentes ou dados insuficientes produzirão limitação de cobertura.
- Preservar evidência verificável, revisão, validade, reavaliação e deduplicação persistente por projeto e vulnerabilidade canônica, reunindo ocorrências entre artefatos sem perder sua identidade.
- Cadastro de projeto não concede acesso adicional nem autoriza remediações; indisponibilidade e incerteza não serão convertidas em ausência de vulnerabilidade.
- A demonstração exigirá integração real e cenários de alteração de advisory, cache, concorrência, retomada e rechecagem após correção, conforme as premissas fornecidas.

## Decisoes do owner
- (nenhuma pergunta bloqueante)

## Aguardando decisao do owner
- [intent] Para IaC, a expansão deve avaliar apenas vulnerabilidades de componentes referenciados ou também configurações inseguras da infraestrutura? (id=q1)
- [intent] SBOM será uma entrada existente para análise ou a entrega também deverá gerar SBOM dos projetos e imagens? (id=q2)
