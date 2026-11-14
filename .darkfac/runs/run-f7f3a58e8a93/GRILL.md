# GRILL

## Premissas
- Esta rodada foi exclusivamente de leitura, sem alterações de arquivos, criação de tickets ou execução de integrações operacionais.
- README, AGENTS.md, FeederCycle, BacklogFeeder e inventário de infraestrutura foram consultados; o inventário de infraestrutura existente não equivale ao inventário de conteúdo de imagens.
- A implementação futura seguirá Python, contratos Pydantic v2 e serviços headless, reutilizando registro de projetos e persistência existentes.
- Serão aceitas referências registry/repositório@sha256; tags observadas serão metadados e não substituirão a identidade por digest.
- A validação seguirá as especificações consultadas: https://github.com/opencontainers/image-spec/blob/main/manifest.md e https://github.com/opencontainers/image-spec/blob/main/image-index.md, com schemaVersion 2 e compatibilidade Docker schema 2, incluindo manifest lists.
- A identidade registrará registry, repositório, digest de entrada, digest do índice quando houver, digest do manifesto selecionado, os/architecture/variant e ambiente; manifesto direto não terá digest de índice inventado.
- A integridade será verificada calculando os digests dos bytes obtidos; o inventário terá serialização determinística e digest próprio, separado dos digests OCI.
- Plataforma desconhecida, ausente ou seleção ambígua produzirá limitação explícita, sem escolher silenciosamente a plataforma do host.
- Registry inacessível, autenticação indisponível, formato incompatível ou conteúdo insuficiente produzirão limitações de cobertura, nunca conclusão de ausência de vulnerabilidades.
- Somente acessos já autorizados serão reutilizados; não será necessário fornecer segredos nesta rodada.
- A resolução e inspeção não executarão containers nem código das imagens; evidências de conteúdo serão vinculadas ao manifesto e à plataforma correspondentes.

## Decisoes do owner
- (nenhuma pergunta bloqueante)

## Aguardando decisao do owner
- [intent] Quais plataformas devem ser inventariadas quando o digest referencia um índice com várias plataformas? (id=q1)
- [intent] O inventário de conteúdo deve identificar componentes instalados ou apenas registrar configuração e camadas da imagem? (id=q2)
