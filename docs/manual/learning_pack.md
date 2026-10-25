<!-- covers: core/learning_pack/delivery.py -->
# Learning pack da entrega

Gerado por codigo, a partir do diff `base..head`, dos testes alterados e do ADR (se houver). Tem dois niveis: leigo (o que mudou, em linguagem simples) e dev (arquivos, funcoes, testes).

Uso:

```powershell
python -m core.learning_pack.delivery --base origin/main --head HEAD --ticket USR-XXX --out C:\caminho\saida
```

Gera `<ticket>.json` (schema Pydantic `LearningPackArtifact`) e `<ticket>.md`.

Verificacao: se um redator (`llm`) for injetado em `build_for_delivery`, cada frase que cita caminho ou simbolo e conferida contra o diff e a arvore do head. Frase com arquivo inexistente, arquivo nao alterado citado como alterado ou simbolo inexistente e removida e registrada em `rejected_claims`. Sem `llm`, o texto vem so dos fatos.
