# Review round 1

Verdict: approve

## Nao-bloqueantes
- `core/harness/affected.py:595` — Fix de shallow/detached checkout no select_affected (e a demanda USR-162 em demands.json) não faz parte do escopo dos tickets T1-T3 (sidebar) nem do 'Arquivos a tocar' da SPEC; é um bundle de outra correção de infraestrutura de validação. (fix: Considerar separar em commit/PR próprio da USR-162 para manter o diff rastreável ao ticket, embora não bloqueie esta entrega já que os testes passam e nada quebra.)
- `hub/frontend/styles.css:193` — .ml-64 (sem prefixo lg:) e .lg\:static são gerados mas não são usados por nenhum elemento em index.html (só lg:ml-64 é aplicado em header/main). (fix: Remover as utilities não referenciadas do gerador, ou deixar como está se antecipam reuso futuro.)
- `.factory/demands/demands.json:4396` — Arquivo perdeu a quebra de linha final (diff mostra '\ No newline at end of file'). (fix: Adicionar newline final ao salvar o JSON.)
