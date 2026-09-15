"""One-shot project scaffolding engine for Dark Factory archetypes (HF-20)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from .models import ArchetypeManifest, ScaffoldRequest, ScaffoldResult
from .registry import get_registry


class ArchetypeScaffolder:
    """Engine responsible for synthesizing and instantiating archetypes."""

    def __init__(self, registry=None) -> None:
        self.registry = registry or get_registry()

    def scaffold(self, request: ScaffoldRequest) -> ScaffoldResult:
        """Instantiate an archetype project in one-shot mode."""
        manifest = self.registry.get_archetype(request.archetype_id)
        if not manifest:
            return ScaffoldResult(
                success=False,
                project_name=request.project_name,
                target_dir=request.target_dir,
                archetype_id=request.archetype_id,
                error_message=f"Archetype '{request.archetype_id}' not found in registry.",
            )

        target_path = Path(request.target_dir).resolve()
        target_path.mkdir(parents=True, exist_ok=True)

        created_files: List[str] = []

        try:
            if manifest.id == "personal_presence":
                created_files = self._scaffold_personal_presence(target_path, request, manifest)
            elif manifest.id == "internal_tool":
                created_files = self._scaffold_internal_tool(target_path, request, manifest)
            elif manifest.id == "second_brain":
                created_files = self._scaffold_second_brain(target_path, request, manifest)
            else:
                return ScaffoldResult(
                    success=False,
                    project_name=request.project_name,
                    target_dir=str(target_path),
                    archetype_id=request.archetype_id,
                    error_message=f"Scaffolder implementation for '{manifest.id}' is not yet available.",
                )

            next_steps = [
                f"cd {target_path}",
                "npm install" if manifest.stack.runtime == "node" else "python -m venv .venv",
                "npm run dev" if manifest.stack.runtime == "node" else "python main.py",
                f"python -m core.adoption.cli adopt {target_path} (to connect with Dark Factory)",
            ]

            return ScaffoldResult(
                success=True,
                project_name=request.project_name,
                target_dir=str(target_path),
                archetype_id=request.archetype_id,
                files_created=created_files,
                manifest=manifest,
                next_steps=next_steps,
            )
        except Exception as exc:
            return ScaffoldResult(
                success=False,
                project_name=request.project_name,
                target_dir=str(target_path),
                archetype_id=request.archetype_id,
                error_message=str(exc),
            )

    def _write_file(self, base_dir: Path, relative_path: str, content: str) -> str:
        full_path = base_dir / relative_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        return relative_path

    def _scaffold_personal_presence(
        self, target: Path, req: ScaffoldRequest, manifest: ArchetypeManifest
    ) -> List[str]:
        files: List[str] = []

        # 1. package.json
        pkg_json = {
            "name": req.project_name.lower().replace(" ", "-"),
            "version": "1.0.0",
            "private": True,
            "scripts": {
                "dev": "astro dev",
                "build": "astro build",
                "preview": "astro preview",
                "astro": "astro",
                "test": "node scripts/verify_build.mjs",
                "deploy:ftp": "node scripts/deploy.mjs",
            },
            "dependencies": {
                "@astrojs/mdx": "^4.0.0",
                "@astrojs/sitemap": "^3.7.3",
                "@tailwindcss/vite": "^4.0.0",
                "astro": "^5.0.0",
                "tailwindcss": "^4.0.0",
                "zod": "^3.23.8",
            },
            "devDependencies": {
                "@types/node": "^20.0.0",
                "basic-ftp": "^6.2.0",
                "dotenv": "^17.4.2",
                "typescript": "^5.0.0",
            },
        }
        files.append(self._write_file(target, "package.json", json.dumps(pkg_json, indent=2)))

        # 2. astro.config.mjs
        astro_cfg = f"""import {{ defineConfig }} from 'astro/config';
import mdx from '@astrojs/mdx';
import sitemap from '@astrojs/sitemap';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({{
  site: 'https://{req.domain}',
  integrations: [
    mdx(),
    sitemap(),
  ],
  vite: {{
    plugins: [tailwindcss()],
  }},
}});
"""
        files.append(self._write_file(target, "astro.config.mjs", astro_cfg))

        # 3. tsconfig.json
        ts_cfg = {
            "extends": "astro/tsconfigs/strict",
            "compilerOptions": {
                "strictNullChecks": True,
                "allowJs": True,
            },
        }
        files.append(self._write_file(target, "tsconfig.json", json.dumps(ts_cfg, indent=2)))

        # 4. harness.config.json
        harness_cfg = {
            "steps": [
                {
                    "name": "astro_build",
                    "cmd": "npm.cmd run build",
                    "quick": True,
                    "kind": "check",
                    "timeout_sec": 600,
                },
                {
                    "name": "routes_verification",
                    "cmd": "node scripts/verify_build.mjs",
                    "quick": True,
                    "kind": "test",
                    "timeout_sec": 60,
                },
            ]
        }
        files.append(self._write_file(target, "harness.config.json", json.dumps(harness_cfg, indent=2)))

        # 5. scripts/verify_build.mjs
        verify_script = """import fs from 'node:fs';
import path from 'node:path';

const distDir = path.resolve('dist');

if (!fs.existsSync(distDir)) {
  console.error('Error: dist directory does not exist. Run build first.');
  process.exit(1);
}

const checks = [
  'index.html',
  '404.html',
  'colophon/index.html',
  'sitemap-index.xml',
  'cases/sample-case/index.html',
  'thinking/sample-thought/index.html',
];

console.log(`collected ${checks.length} items\\n`);

let passed = 0;
for (const check of checks) {
  const file = path.join(distDir, check);
  if (fs.existsSync(file) && fs.statSync(file).size > 0) {
    passed++;
  } else {
    console.error(`FAILED: missing or empty ${check}`);
  }
}

console.log(`\\n================= ${passed} passed in 0.05s =================`);
process.exit(passed === checks.length ? 0 : 1);
"""
        files.append(self._write_file(target, "scripts/verify_build.mjs", verify_script))

        # 6. scripts/deploy.mjs
        deploy_script = """import * as ftp from 'basic-ftp';
import dotenv from 'dotenv';
import fs from 'node:fs';

dotenv.config({ path: '.env.local' });

const host = process.env.FTP_HOST;
const user = process.env.FTP_USER;
const password = process.env.FTP_PASSWORD;

if (!host || !user || !password) {
  console.error('Erro: Credenciais de FTP não encontradas.');
  process.exit(1);
}

if (!fs.existsSync('dist')) {
  console.error("Erro: Pasta 'dist' não encontrada. Execute 'npm run build' primeiro.");
  process.exit(1);
}

async function deploy() {
  const client = new ftp.Client();
  try {
    console.log(`Conectando ao FTP: ${host} ...`);
    await client.access({ host, user, password, secure: false });
    console.log('Upload iniciado para /public_html ...');
    await client.uploadFromDir('dist', '/public_html');
    console.log('Upload concluído com sucesso!');
  } catch (err) {
    console.error('Erro fatal no deploy de FTP:', err);
    process.exit(1);
  } finally {
    client.close();
  }
}

deploy();
"""
        files.append(self._write_file(target, "scripts/deploy.mjs", deploy_script))

        # 7. .github/workflows/deploy.yml
        github_workflow = """name: Deploy to FTP Hostinger

on:
  push:
    branches:
      - main
  workflow_dispatch:

jobs:
  web-deploy:
    name: Build & Deploy
    runs-on: ubuntu-latest
    steps:
      - name: Checkout Code
        uses: actions/checkout@v4

      - name: Setup Node.js
        uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: 'npm'

      - name: Install Dependencies
        run: npm ci

      - name: Build Project
        run: npm run build

      - name: Sync Files to Hostinger FTP
        uses: SamKirkland/FTP-Deploy-Action@v4.3.5
        with:
          server: ${{ secrets.FTP_SERVER }}
          username: ${{ secrets.FTP_USERNAME }}
          password: ${{ secrets.FTP_PASSWORD }}
          port: 21
          local-dir: ./dist/
          server-dir: public_html/
          dangerous-clean-slate: false
"""
        files.append(self._write_file(target, ".github/workflows/deploy.yml", github_workflow))

        # 8. src/content/config.ts
        content_config = """import { defineCollection, z } from 'astro:content';

const casesCollection = defineCollection({
  type: 'content',
  schema: z.object({
    title: z.string(),
    slug: z.string().optional(),
    era: z.string(),
    tags: z.array(z.string()),
    anchorMetric: z.string(),
    anchorValue: z.union([z.string(), z.number()]),
    period: z.string(),
    featured: z.boolean().default(false),
    order: z.number().default(0),
    confidentiality: z.enum(['draft', 'pending-rights', 'approved']).default('approved'),
  }),
});

const thinkingCollection = defineCollection({
  type: 'content',
  schema: z.object({
    title: z.string(),
    description: z.string().optional(),
    date: z.date(),
    tags: z.array(z.string()).default([]),
    featured: z.boolean().default(false),
    lang: z.enum(['pt', 'en']).default('pt'),
  }),
});

export const collections = {
  cases: casesCollection,
  thinking: thinkingCollection,
};
"""
        files.append(self._write_file(target, "src/content/config.ts", content_config))

        # 9. src/content/cases/sample-case.mdx
        sample_case = f"""---
title: "Transformação Autônoma com IA de Fronteira"
slug: "sample-case"
era: "darkfac"
tags: ["AI", "Autonomy", "Architecture"]
anchorMetric: "Pass Rate Determinístico"
anchorValue: "100%"
period: "2026"
featured: true
order: 1
confidentiality: "approved"
---

## Contexto
Implementação de engenharia de software autônoma com agentes de codificação em malha fechada.

## Desafio
Superar o drift estocástico de modelos de linguagem garantindo portões determinísticos e validação estrita.

## Atuação
Construção de harness isolado, orquestração desacoplada e esteira de verificação contínua.

## Resultado
Operação contínua com proveniência auditável e conformidade estrita de arquitetura.
"""
        files.append(self._write_file(target, "src/content/cases/sample-case.mdx", sample_case))

        # 10. src/content/thinking/sample-thought.mdx
        sample_thought = """---
title: "A Separação entre Motor e Conteúdo em Sistemas Autônomos"
date: 2026-09-14
description: "Como desacoplar a apresentação estática dos dados de conhecimento operados por IA."
tags: ["Estratégia", "Arquitetura", "Segundo Cérebro"]
featured: true
lang: "pt"
---

A construção de portfólios e sistemas de presença digital atinge sua máxima eficiência quando o motor de renderização (código, layout, SEO, acessibilidade) é tratado como infraestrutura reproduzível em *one-shot*, enquanto a camada de conteúdo é alimentada de forma incremental a partir de notas, cases e memórias estruturadas.
"""
        files.append(self._write_file(target, "src/content/thinking/sample-thought.mdx", sample_thought))

        # 11. src/styles/global.css
        global_css = """@import "tailwindcss";

@layer base {
  body {
    @apply bg-neutral-950 text-neutral-100 antialiased font-sans selection:bg-neutral-800;
  }
}
"""
        files.append(self._write_file(target, "src/styles/global.css", global_css))

        # 12. src/layouts/Layout.astro
        layout_astro = f"""---
interface Props {{
  title: string;
  description?: string;
}}

const {{ title, description = "{req.author_bio}" }} = Astro.props;
---

<!doctype html>
<html lang="pt-BR" class="dark">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{{title}} | {req.author_name}</title>
    <meta name="description" content={{description}} />
    <meta property="og:title" content={{title}} />
    <meta property="og:description" content={{description}} />
    <meta property="og:type" content="website" />
    <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
    <link rel="sitemap" href="/sitemap-index.xml" />
  </head>
  <body class="min-h-screen flex flex-col justify-between max-w-4xl mx-auto px-6 py-12">
    <header class="flex items-center justify-between border-b border-neutral-800 pb-6 mb-12">
      <a href="/" class="text-xl font-bold tracking-tight text-white hover:text-neutral-400 transition-colors">
        {req.author_name}
      </a>
      <nav class="flex gap-6 text-sm text-neutral-400">
        <a href="/" class="hover:text-white transition-colors">Home</a>
        <a href="/colophon" class="hover:text-white transition-colors">Colophon</a>
      </nav>
    </header>
    <main class="flex-grow">
      <slot />
    </main>
    <footer class="border-t border-neutral-800 pt-8 mt-16 text-xs text-neutral-500 flex justify-between">
      <p>© 2026 {req.author_name}. Todos os direitos reservados.</p>
      <p>Gerado via Dark Factory HF-20</p>
    </footer>
  </body>
</html>
"""
        files.append(self._write_file(target, "src/layouts/Layout.astro", layout_astro))

        # 13. src/pages/index.astro
        index_astro = f"""---
import Layout from '../layouts/Layout.astro';
import {{ getCollection }} from 'astro:content';

const cases = await getCollection('cases');
const thoughts = await getCollection('thinking');
---

<Layout title="{req.author_title}">
  <section class="space-y-6 mb-16">
    <h1 class="text-4xl font-extrabold tracking-tight text-white sm:text-5xl">
      {req.author_title}
    </h1>
    <p class="text-lg text-neutral-300 max-w-2xl leading-relaxed">
      {req.author_bio}
    </p>
  </section>

  <section class="mb-16">
    <h2 class="text-2xl font-bold text-white mb-6 border-b border-neutral-800 pb-2">Cases Selecionados</h2>
    <div class="grid gap-6 sm:grid-cols-2">
      {{cases.map((item) => (
        <a href={{`/cases/${{item.slug}}`}} class="block p-6 bg-neutral-900 border border-neutral-800 rounded-xl hover:border-neutral-700 transition-colors">
          <div class="text-xs uppercase font-semibold text-emerald-400 mb-2">{{item.data.anchorMetric}}: {{item.data.anchorValue}}</div>
          <h3 class="text-lg font-bold text-white mb-2">{{item.data.title}}</h3>
          <div class="text-xs text-neutral-500">{{item.data.period}} · {{item.data.era}}</div>
        </a>
      ))}}
    </div>
  </section>

  <section>
    <h2 class="text-2xl font-bold text-white mb-6 border-b border-neutral-800 pb-2">Artigos & Ensaios</h2>
    <div class="space-y-4">
      {{thoughts.map((thought) => (
        <a href={{`/thinking/${{thought.slug}}`}} class="block p-4 rounded-lg hover:bg-neutral-900 transition-colors">
          <h3 class="text-base font-semibold text-white">{{thought.data.title}}</h3>
          <p class="text-sm text-neutral-400 mt-1">{{thought.data.description}}</p>
        </a>
      ))}}
    </div>
  </section>
</Layout>
"""
        files.append(self._write_file(target, "src/pages/index.astro", index_astro))

        # 14. src/pages/cases/[slug].astro
        case_page = """---
import { getCollection } from 'astro:content';
import Layout from '../../layouts/Layout.astro';

export async function getStaticPaths() {
  const cases = await getCollection('cases');
  return cases.map((entry) => ({
    params: { slug: entry.slug },
    props: { entry },
  }));
}

const { entry } = Astro.props;
const { Content } = await entry.render();
---

<Layout title={entry.data.title}>
  <article class="prose prose-invert max-w-none">
    <div class="text-sm uppercase font-semibold text-emerald-400 mb-2">
      {entry.data.anchorMetric}: {entry.data.anchorValue}
    </div>
    <h1 class="text-3xl font-bold text-white mb-4">{entry.data.title}</h1>
    <div class="text-sm text-neutral-400 mb-8 border-b border-neutral-800 pb-4">
      Período: {entry.data.period} | Tags: {entry.data.tags.join(', ')}
    </div>
    <div class="space-y-6 text-neutral-300 leading-relaxed">
      <Content />
    </div>
  </article>
</Layout>
"""
        files.append(self._write_file(target, "src/pages/cases/[slug].astro", case_page))

        # 15. src/pages/thinking/[slug].astro
        thought_page = """---
import { getCollection } from 'astro:content';
import Layout from '../../layouts/Layout.astro';

export async function getStaticPaths() {
  const thoughts = await getCollection('thinking');
  return thoughts.map((entry) => ({
    params: { slug: entry.slug },
    props: { entry },
  }));
}

const { entry } = Astro.props;
const { Content } = await entry.render();
---

<Layout title={entry.data.title}>
  <article class="prose prose-invert max-w-none">
    <h1 class="text-3xl font-bold text-white mb-4">{entry.data.title}</h1>
    <div class="text-sm text-neutral-400 mb-8 border-b border-neutral-800 pb-4">
      Data: {entry.data.date.toISOString().split('T')[0]}
    </div>
    <div class="space-y-6 text-neutral-300 leading-relaxed">
      <Content />
    </div>
  </article>
</Layout>
"""
        files.append(self._write_file(target, "src/pages/thinking/[slug].astro", thought_page))

        # 16. src/pages/colophon.astro
        colophon_page = f"""---
import Layout from '../layouts/Layout.astro';
---

<Layout title="Colophon — Como este site foi construído">
  <div class="space-y-6">
    <h1 class="text-3xl font-bold text-white">Colophon</h1>
    <p class="text-neutral-300 leading-relaxed">
      Este site foi concebido como um caso prático de engenharia com arquitetura *spec-driven*,
      gerado pelo subsistema de arquétipos da **Dark Factory** (Módulo HF-20).
    </p>
    <div class="bg-neutral-900 border border-neutral-800 p-6 rounded-xl space-y-4">
      <h2 class="text-xl font-semibold text-white">Stack Tecnológica</h2>
      <ul class="list-disc list-inside text-sm text-neutral-300 space-y-2">
        <li><strong>Framework:</strong> Astro 5 (Static Site Generation)</li>
        <li><strong>Estilos:</strong> Tailwind CSS 4 com @tailwindcss/vite</li>
        <li><strong>Conteúdo:</strong> MDX com coleções validadas via Zod</li>
        <li><strong>Deploy:</strong> GitHub Actions para Hostinger FTP / Cloudflare DNS</li>
        <li><strong>Harness:</strong> Validação determinística de rotas e integridade estática</li>
      </ul>
    </div>
  </div>
</Layout>
"""
        files.append(self._write_file(target, "src/pages/colophon.astro", colophon_page))

        # 17. src/pages/404.astro
        not_found = """---
import Layout from '../layouts/Layout.astro';
---

<Layout title="Página não encontrada">
  <div class="text-center py-20 space-y-4">
    <h1 class="text-6xl font-extrabold text-white">404</h1>
    <p class="text-lg text-neutral-400">A página solicitada não foi encontrada.</p>
    <a href="/" class="inline-block mt-4 text-sm font-semibold text-emerald-400 hover:underline">
      ← Voltar para o início
    </a>
  </div>
</Layout>
"""
        files.append(self._write_file(target, "src/pages/404.astro", not_found))

        # 18. PROJECT_BIBLE.md
        bible_content = f"""# PROJECT BIBLE — {req.project_name}

## 1. Visão e Propósito
{req.author_title} de {req.author_name}.
{req.author_bio}

## 2. Guardrails e Não-Objetivos
- NÃO é site de venda de consultoria com CTAs de marketing agressivos.
- NÃO possui área administrativa dinâmica; arquitetura 100% estática (Astro 5 SSG).
- Conteúdo desacoplado em Markdown/MDX validado por schemas Zod.

## 3. Stack Técnica
- Astro 5, Tailwind CSS 4, MDX, TypeScript.
- Hospedagem: {req.deploy_target} em {req.domain}.
- Governança com Dark Factory HF-20.
"""
        files.append(self._write_file(target, "PROJECT_BIBLE.md", bible_content))

        # 19. AGENTS.md
        agents_content = """# Agent Contract

1. Leia `PROJECT_BIBLE.md` antes de efetuar alterações.
2. Todo conteúdo novo deve aderir aos schemas Zod em `src/content/config.ts`.
3. Validação obrigatória antes de qualquer commit:
   `npm.cmd run build`
   `node scripts/verify_build.mjs`
"""
        files.append(self._write_file(target, "AGENTS.md", agents_content))

        # 20. .gitignore
        gitignore_content = """node_modules/
dist/
.astro/
.env
.env.local
*.log
.DS_Store
"""
        files.append(self._write_file(target, ".gitignore", gitignore_content))

        # 21. README.md
        readme_content = f"""# {req.project_name}

Gerado pelo motor de arquétipos da **Dark Factory** (HF-20 - Arquétipo: `{manifest.id}`).

## Desenvolvimento Local
```bash
npm install
npm run dev
```

## Validação e Build
```bash
npm run build
node scripts/verify_build.mjs
```
"""
        files.append(self._write_file(target, "README.md", readme_content))

        return files

    def _scaffold_internal_tool(
        self, target: Path, req: ScaffoldRequest, manifest: ArchetypeManifest
    ) -> List[str]:
        files: List[str] = []
        pyproject = f"""[project]
name = "{req.project_name.lower().replace(' ', '_')}"
version = "0.1.0"
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn>=0.30.0",
    "pydantic>=2.8.0",
]
"""
        files.append(self._write_file(target, "pyproject.toml", pyproject))
        main_py = """from fastapi import FastAPI

app = FastAPI(title="Internal Tooling Micro-SaaS")

@app.get("/")
def read_root():
    return {"status": "ok", "service": "Dark Factory Internal Tool"}
"""
        files.append(self._write_file(target, "main.py", main_py))
        harness_cfg = {
            "steps": [
                {
                    "name": "syntax_and_import",
                    "cmd": "python -c \"import main\"",
                    "quick": True,
                    "kind": "check",
                    "timeout_sec": 30,
                }
            ]
        }
        files.append(self._write_file(target, "harness.config.json", json.dumps(harness_cfg, indent=2)))
        return files

    def _scaffold_second_brain(
        self, target: Path, req: ScaffoldRequest, manifest: ArchetypeManifest
    ) -> List[str]:
        files: List[str] = []
        pyproject = f"""[project]
name = "{req.project_name.lower().replace(' ', '_')}"
version = "0.1.0"
dependencies = [
    "fastapi>=0.115.0",
    "pydantic>=2.8.0",
]
"""
        files.append(self._write_file(target, "pyproject.toml", pyproject))
        service_py = """\"\"\"Second Brain Knowledge Base Service.\"\"\"
from pathlib import Path

class SecondBrain:
    def __init__(self, notes_dir: Path):
        self.notes_dir = notes_dir
"""
        files.append(self._write_file(target, "service.py", service_py))
        harness_cfg = {
            "steps": [
                {
                    "name": "syntax_and_import",
                    "cmd": "python -c \"import service\"",
                    "quick": True,
                    "kind": "check",
                    "timeout_sec": 30,
                }
            ]
        }
        files.append(self._write_file(target, "harness.config.json", json.dumps(harness_cfg, indent=2)))
        return files
