# GRILL

## Premissas
- A rodada permanece exclusivamente de leitura, sem criação de arquivos, tickets ou execução de integrações.
- README, AGENTS.md e código de infraestrutura e coordenação foram consultados; o inventário em core/infra representa máquinas e serviços, não componentes de software vulneráveis.
- Este marco cobre apenas vulnerabilidades de componentes referenciados; configuração insegura, misconfiguration e policy-as-code ficam excluídos.
- Providers Terraform serão identificados pelo source canônico e pela versão selecionada no .terraform.lock.hcl, preservando hashes declarados e vínculo aos arquivos HCL2; restrições de versão isoladas não comprovam resolução.
- Módulos Terraform só serão avaliados com origem identificável e versão exata ou referência imutável verificável; tags e branches Git isoladas não comprovam imutabilidade.
- Helm será limitado a Chart.yaml com apiVersion v2 e Chart.lock associado, preservando origem, versão resolvida e evidência disponível; appVersion não será usado como versão das dependências nem como prova de conteúdo das imagens.
- A leitura dos manifestos não executará Terraform, Helm, scripts ou downloads de dependências; evidência insuficiente produzirá limitação explícita.
- Hashes declarados serão preservados como evidência, sem afirmar que os artefatos foram baixados ou sua integridade verificada.
- Referências sem versão exata, arquivos inconsistentes e componentes sem correspondência confiável no domínio canônico produzirão limitações de cobertura, sem inventar findings negativos nem declarar ausência de vulnerabilidade.
- Node, imagens e geração de SBOM pertencem ao contexto mais amplo, mas não ampliam esta entrega pontual de inventário IaC.
- (technical) Onde está a base entregue do analisador de vulnerabilidades e seu domínio canônico, que não foram localizados neste checkout? -> **Identificar e reconciliar a base como dependência antes de planejar a extensão** (A demanda pressupõe contratos existentes; recriá-los ampliaria o escopo e poderia duplicar identidade, avaliação e persistência.)

## Decisoes do owner
- (nenhuma pergunta bloqueante)
