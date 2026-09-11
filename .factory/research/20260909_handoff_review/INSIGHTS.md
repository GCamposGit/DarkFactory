# Validação Pydantic e fronteiras de confiança

Consulta limitada à documentação oficial; não é benchmarking nem pesquisa de alternativas.

Strict e extra=forbid resolvem propriedades diferentes. A rota JSON pode aceitar representações diferentes da rota Python. O reparo de HF-02 deve testar o arquivo gerado no processo consumidor.

Construção/cópia de modelos não fornece autoridade para claims ou receipts. O supervisor deve resolver origem e vínculos antes de o gate autorizar avanço. Essa decisão arquitetural é uma inferência da revisão local, não uma garantia oferecida pelo Pydantic.

Fontes: [Strict Mode](https://docs.pydantic.dev/latest/concepts/strict_mode/) e [Models](https://docs.pydantic.dev/latest/concepts/models/). Consultadas em 09/09/2026.
