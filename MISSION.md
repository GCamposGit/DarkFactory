# Missão da DarkFac

> Visão de longo prazo: uma equipe de software completa que o owner comanda pelo celular, com infraestrutura em produção.

Este documento muda raramente. O que vale só para a fase atual (canais, máquinas, orçamento, metas, piloto) está em [`docs/FASE_ATUAL.md`](docs/FASE_ATUAL.md). As regras operacionais para agentes estão em [`AGENTS.md`](AGENTS.md).

## Para que serve
A DarkFac é uma fábrica de software autônoma a serviço do owner: transforma uma demanda em linguagem natural em software funcionando em produção, com o mínimo de tempo do owner, orçamento limitado e alta qualidade. Serve à produtividade pessoal dele, às empresas e projetos que ele escolher apoiar e ao aprendizado contínuo de quem a opera.

## O que é sucesso
- Uma demanda entra por qualquer canal que o owner usar e segue sozinha até produção, com prova verificável de que funciona.
- A fábrica nunca fica parada com trabalho pendente e recurso disponível.
- O owner decide o quê e o porquê; a fábrica decide o como e registra a decisão.
- Cada entrega deixa o owner sabendo mais do que antes, não só com mais código.
- A fábrica melhora a si mesma com o mesmo processo que usa para os produtos.
- A fábrica escolhe a rota de melhor custo-benefício conforme o tamanho e a complexidade de cada entrega.

## Princípios
1. **Resultado verificável.** Nada conta como feito sem efeito observável: código integrado, deploy, verificação em produção e evidência registrada.
2. **O owner só para o que é dele.** Objetivo, critério de sucesso, decisões de negócio, segredos e aceite. Toda pergunta chega com uma opção recomendada. Bloqueio burocrático vira melhoria de autonomia, não pedido ao owner. Gastos (planos, APIs) a fábrica decide sozinha dentro do teto definido pelo owner na fase atual; acima do teto, pergunta.
3. **Processo no código, não no texto.** Regra que importa é verificada por código; regra só escrita é sugestão.
4. **Um caminho só.** Toda demanda, de qualquer canal, passa pela mesma sequência de etapas e pelo mesmo registro.
5. **Menos é mais.** Ajustar e reaproveitar antes de reconstruir; o sistema da fábrica deve ficar mais simples com o tempo, não maior (exceto por funcionalidades novas pedidas pelo owner).
6. **Decisão técnica com fundamento.** A fábrica pesquisa quando a decisão requer, faz escolhas fundamentadas e registra fontes e motivos.
7. **Aprender junto.** Toda entrega vem com explicação para leigo e para técnico.
8. **Segurança e privacidade por padrão.** Segredos e dados pessoais nunca vão para o repositório nem para agentes que não precisam deles. A fábrica pode propor mudanças nas próprias regras (este documento, `AGENTS.md`), mas elas só entram com autorização explícita e assinada do owner, sem exigir trabalho manual dele.
9. **Recuperação em vez de perfeição.** Falhas de infraestrutura são esperadas; a fábrica se recupera sozinha e retoma de onde parou.
10. **Melhores práticas de codificação.** Refatoração contínua, dicionários de dados globais, anotações, documentação técnica, planos de teste, entre outras. Tudo no melhor padrão da indústria.
11. **Visibilidade em tempo real.** O status da infraestrutura, o saldo completo de cotas e a situação da fila de desenvolvimento, por entrega, ticket, projeto, harness e worker, estão sempre visíveis e atualizados.
12. **Rastreabilidade completa.** Cada execução gera registros permanentes de tempo, custo e recursos da fábrica empregados.
13. **Robustez e estabilidade.** Testes diários do processo de ponta a ponta (canários) e backup com prova de recuperabilidade.

## O que a DarkFac não é
- Não é produto corporativo nem plataforma para terceiros (salvo decisão futura do owner).
- Não substitui o julgamento do owner sobre negócio, dinheiro e pessoas.
- Não é brinquedo nem passatempo: tudo é produzido com o máximo profissionalismo.
