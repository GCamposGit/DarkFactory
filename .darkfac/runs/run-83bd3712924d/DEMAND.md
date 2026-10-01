# DEMAND

- projeto: darkfac
- canal: line

## Texto original

# Transformar menu superior em menu vertical lateral esquerdo no DarkHub

## Problema
Transformar o menu superior, que está indo muito para a direita, em um menu vertical no lado esquerdo do site.

[Plano HF-28 | 2026-09-30] PILOTO V1 (nao implementar em chat): este ticket e o veiculo de verificacao do criterio V1 do plano da linha (demanda -> PR -> CI -> merge -> deploy -> smoke sem toque humano). So deve ser submetido a linha (Telegram /linha USR-62 ou dogfood, USR-92) DEPOIS de USR-84, USR-85 e USR-86, pois enquanto o CI do darkfac estiver vermelho a etapa de integracao nunca fecha o PR.

## Jornada
1. O usuário solicita a operação relacionada a: Transformar menu superior em menu vertical lateral esquerdo no DarkHub
2. O sistema executa a regra de negócio headless e emite o resultado esperado
3. A interface do DarkHub reflete o estado atualizado com sucesso

## Fora de escopo
- Não adicionar dependências externas pesadas sem necessidade estrita
- Não modificar modelos de dados ou arquivos fora do escopo desta demanda
- Não acoplar lógica de negócios diretamente à apresentação gráfica

## Criterios de aceite
- A funcionalidade descrita em 'Transformar menu superior em menu vertical lateral esquerdo no DarkHub' opera sem erros sintáticos ou de tipos.
- Validação headless executada com sucesso via `python -m pytest tests/test_transformar_menu_superior_em_m.py -v`.
- O status e histórico do ticket são atualizados no backlog da Dark Factory.
