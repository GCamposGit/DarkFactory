---
name: visual-asset-studio
description: Ateliê de geração de ativos visuais e ilustrações inteligentes para software e publicações técnicas. Produz banners sociais (LinkedIn/Twitter), heroes de blog, diagramas de arquitetura, mockups de UI, ícones e ilustrações conceituais. Integra acoplamento semântico automático com a Skill 15 (ilustra textos e posts) e arquitetura híbrida de custo zero (renderizador procedural vetorial/raster local) com escalabilidade para modelos de fronteira em nuvem (Gemini 2.5 Flash Image, Flux, DALL-E 3).
---

# 16 - Visual Asset & Context-Aware Image Studio

O **Visual Asset Studio** é o motor de geração de ativos gráficos e ilustrações técnicas da Dark Factory, combinando renderização procedural determinística local (Pillow, custo zero) com suporte a modelos neurais de imagem em nuvem.

---

## 1. Contratos Normativos da Etapa

### Inputs (Entradas)
- **Especificação do Ativo**: Título, formato desejado e tema estético.
- **Formatos Suportados**: `social_banner`, `blog_hero`, `architecture_diagram`, `ui_mockup` ou `app_icon`.
- **Tema Visual**: `modern_minimalist_dark`, `cyberpunk_terminal`, `blueprint_technical`, `clean_vector_3d` ou `glassmorphism`.
- **Texto para Acoplamento Semântico (Opcional)**: Trecho de artigo técnico ou post da Skill 15 para síntese automática de arte visual.

### Ações e Procedimento Executável
1. **Geração Procedural Determinística Local (Custo $0, Offline)**:
   - Crie imagens técnicas de alta resolução via Pillow em milissegundos:
     ```powershell
     python -m core.visual.cli create --title "DarkFac Autonomous Engine" --type social_banner --theme modern_minimalist_dark --offline
     ```
2. **Ilustração Automática Acoplada ao Texto da Skill 15**:
   ```powershell
   python -m core.visual.cli illustrate --text "Texto do blog técnico..." --type blog_hero --offline
   ```
3. **Diagramação Arquitetural Rápida**:
   ```powershell
   python -m core.visual.cli diagram --title "Distributed Agent Event Bus"
   ```

### Outputs Estruturados
- **Artefato de Imagem**: Arquivo gráfico persistido em `.factory/visuals/<id>.png` (ou `.svg`).
- **Metadados do Ativo**: `.factory/visuals/metadata/<id>.json` registrando dimensões, aspecto, tema e parâmetros de síntese.
- **Disponibilização via HTTP**: Acesso direto no DarkHub via rota `/visuals/<id>.png`.

### Portões, Política e Validação
- **Garantia Local-First**: O ateliê visual opera 100% offline sem falhar na ausência de chaves de API pagas de geração de imagens.
- **Sanitização de Dados**: Nenhum segredo, DSN, token ou informação confidencial pode ser incluído no texto renderizado em diagramas ou mockups.
- **Determinismo Visual**: Renderizações de diagramas de arquitetura devem preservar layout e estados canônicos.

---

## 2. Continuous Self-Improvement & RCA Visual

- **RCA de Legibilidade e Contraste**: Se elementos tipográficos gerados apresentarem sobreposição ou baixo contraste de cor, execute ajuste nas paletas do motor procedural e registre a correção no ledger.
