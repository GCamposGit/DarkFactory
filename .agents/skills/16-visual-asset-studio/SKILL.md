---
name: visual-asset-studio
description: Ateliê de geração de ativos visuais e ilustrações inteligentes para software e publicações técnicas. Produz banners sociais (LinkedIn/Twitter), heroes de blog, diagramas de arquitetura, mockups de UI, ícones e ilustrações conceituais. Integra acoplamento semântico automático com a Skill 15 (ilustra textos e posts) e arquitetura híbrida de custo zero (renderizador procedural vetorial/raster local) com escalabilidade para modelos de fronteira em nuvem (Gemini 2.5 Flash Image, Flux, DALL-E 3).
---

# 16 - Visual Asset & Context-Aware Image Studio

O **Visual Asset Studio** é o braço visual e gráfico da Dark Factory.
Nenhum software moderno ou artigo técnico atinge seu potencial máximo sem uma camada visual de alto impacto: banners para redes profissionais, diagramas de arquitetura cristalinos, mockups de interface e ícones de produto.
Este motor foi projetado com filosofia **Local-First & Custo Zero ($0)**: utiliza um renderizador procedural determinístico de alta estética (Pillow) para criar imagens técnicas em milissegundos sem conexão de rede, escalando para modelos neurais de imagem em nuvem quando chaves de API estiverem disponíveis.

---

## 🏛️ Arquitetura do Ateliê Visual

```text
       ┌─────────────────────────────────────────────────────────────┐
       │             ENTRADA / REQUISITO VISUAL                      │
       │  - Opção A: Especificação Explícita (Título, Tipo, Tema)    │
       │  - Opção B: Acoplamento com Texto da Skill 15 (Auto-Prompt) │
       └──────────────────────────────┬──────────────────────────────┘
                                      │
       ┌──────────────────────────────▼──────────────────────────────┐
       │            SYNTHESIZER SEMÂNTICO DE PROMPTS                 │
       │  - Extração de âncoras temáticas e metáforas conceituais    │
       │  - Inferência de Tema Estético e Aspect Ratio Ideal         │
       │  - Construção de Prompt Neural com Negative Constraints     │
       └──────────────────────────────┬──────────────────────────────┘
                                      │
                         [Modo Offline ou Sem Chave?]
                       ┌──────────────┴──────────────┐
                    Sim│                             │Não (Chave ativa)
       ┌──────────────▼──────────────┐┌──────────────▼──────────────┐
       │   MOTOR PROCEDURAL LOCAL    ││   MODELOS DE FRONTEIRA CLOUD│
       │  - Custo $0, Latência <100ms││  - Gemini 2.5 Flash Image   │
       │  - Renderização via Pillow  ││  - OpenAI DALL-E 3 / Flux   │
       │  - Grades, Nós & Diagramas  ││  - Fotorrealismo e 3D Render│
       └──────────────┬──────────────┘└──────────────┬──────────────┘
                      └──────────────┬───────────────┘
                                     │
       ┌─────────────────────────────▼───────────────────────────────┐
       │             ARTEFATO VISUAL FINAL (.PNG / .SVG)             │
       │  - Salvo em .factory/visuals/                               │
       │  - Servido via HTTP no DarkHub em /visuals/<id>.png         │
       │  - Metadados e catálogo em /visuals/metadata/<id>.json      │
       └─────────────────────────────────────────────────────────────┘
```

---

## 🎯 Os 5 Formatos Nativos de Ativos

1. **📱 Social Banners (`social_banner`)**:
   - Aspect ratio 16:9 ou 4:5. Grades de perspectiva modernas, iluminação volumétrica e cartões tipográficos com badges técnicas.
2. **📰 Blog & Article Heroes (`blog_hero`)**:
   - Imagens de cabeçalho com forte impacto visual e composição cinematográfica equilibrada.
3. **📐 Diagramas de Arquitetura (`architecture_diagram`)**:
   - Diagramas com caixas modulares, barramentos de mensageria, setas direcionais e marcadores de estado (*READY*, *ROUTED*, *ACTIVE*, *VERIFIED*).
4. **💻 Mockups de UI / Dashboard (`ui_mockup`)**:
   - Janelas de aplicação completas com header estilo dark mode, barra de navegação lateral, métricas analíticas e gráficos em tempo real.
5. **🔷 Ícones de App & Badges (`app_icon`)**:
   - Formato quadrado 1:1, squircles com gradientes radiais, iluminação biselada e monogramas geométricos.

---

## 🎨 Temas Estéticos Pré-Configurados

- `modern_minimalist_dark`: Azul ardósia profundo, acentos ciano neon, elegância corporativa sênior.
- `cyberpunk_terminal`: Preto absoluto, fósforo esmeralda estilo terminal, estética hacker de baixo nível.
- `blueprint_technical`: Azul da Prússia profundo, linhas de perspectiva laser brancas e grades de engenharia.
- `clean_vector_3d`: Render isométrico fosco, formas geométricas pastel violeta e cobalto.
- `glassmorphism`: Camadas translúcidas de vidro fosco com destaques especulares e aberração cromática suave.

---

## 🛠️ Comandos da CLI (`core/visual/cli.py`)

### 1. Gerar Banner Social
```powershell
python -m core.visual.cli create \
  --title "DarkFac Level 3 Autonomous Engine" \
  --type social_banner \
  --theme modern_minimalist_dark \
  --offline
```

### 2. Ilustrar um Texto / Post Automaticamente (Coupling com Skill 15)
```powershell
python -m core.visual.cli illustrate \
  --text "Most teams overcomplicate Deterministic Test Harness in Level 3 Software Factory. Here is what actually works in production." \
  --type blog_hero \
  --offline
```

### 3. Gerar Diagrama de Arquitetura Rápido
```powershell
python -m core.visual.cli diagram --title "Distributed Agent Event Bus"
```

### 4. Gerar Mockup de UI
```powershell
python -m core.visual.cli mockup --title "Agent Operations Control Center"
```

### 5. Gerar Ícone de Aplicativo
```powershell
python -m core.visual.cli icon --title "DarkFac"
```

---

## 🤖 Protocolo para Agentes na Dark Factory

Ao entregar novos épicos, whitepapers, releases ou páginas web:
1. Gere o texto purificado usando a Skill 15.
2. Acople a ilustração visual invocando:
   ```python
   from core.visual import VisualStudio
   studio = VisualStudio()
   asset = studio.illustrate_text(text_content=texto_gerado, offline=True)
   ```
3. Exiba o link do arquivo gerado (`.factory/visuals/...`) para visualização imediata do operador.
