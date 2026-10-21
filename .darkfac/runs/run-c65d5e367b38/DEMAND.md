# DEMAND

- projeto: darkfac
- canal: line

## Texto original

# Provar entrega autonoma de piloto greenfield stand alone em repositorio e ambiente privados separados

## Problema
Decisao explicita do owner em 16/10/2026: piloto tecnico pequeno e descartavel; criar repositorio privado na sua conta GitHub e ambiente separado na infraestrutura existente, sem novas contratacoes. Provar a fabrica recebendo uma demanda de projeto novo ate publicacao e verificacao, com minima intervencao e sem dependencia de runtime do core para o produto funcionar. Fatiar execucao em marcos pequenos antes de implementar. Fatia 1 (plano do piloto) em docs/plans/USR-279-greenfield-pilot.md; marcos M1-M7 seguem.

[Decisao do owner OA-042] Decidir o piloto greenfield do USR-279 (nome, texto, destino, retencao, build) -> opcao recomendado (Aceitar todas as recomendacoes), em 2026-10-20T10:29:29Z. Nota do owner: Owner no chat em 20/10/2026: sim (D1 pilot-hello, D2 'DarkFac pilot', D3 opcao A Dokploy existente, D4 manter arquivado, D5 build no venv).

## Jornada
Provar entrega autonoma de piloto greenfield stand alone em repositorio e ambiente privados separados

## Fora de escopo
- Nao contratar servicos novos.
- Nao inventar decisoes de negocio do produto.

## Criterios de aceite
- Planejamento registra o piloto, fronteiras do produto, ambiente, validacao, destino permitido e marcos executaveis de ate tres criterios cada, sem nova decisao de negocio inventada
- Demanda percorre intake, planejamento, implementacao, revisao, PR, CI, merge, deploy e smoke do projeto com evidencias reais vinculadas ao SHA
- Clone do produto instala, testa, constroi e executa sem checkout ou servico DarkFac; falhas sao retomadas ou diagnosticadas e o owner recebe relatorio final
