"""
Generator for DarkHub local self-contained stylesheet (Ticket DH-13 / USR-49).
Compiles a robust, self-contained stylesheet replacing Tailwind CDN.
Covers all utility classes, typography, colors, layout, flexbox, grid, borders,
and responsive variants used across hub/frontend/index.html and JS files.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "hub" / "frontend"
STATIC_DIR = FRONTEND_DIR / "static"

# Standard Tailwind Color Palette (RGB tuples for opacity support)
PALETTES: Dict[str, Dict[str, str]] = {
    "slate": {
        "50": "248, 250, 252",
        "100": "241, 245, 249",
        "200": "226, 232, 240",
        "300": "203, 213, 225",
        "400": "148, 163, 184",
        "500": "100, 116, 139",
        "600": "71, 85, 105",
        "700": "51, 65, 85",
        "800": "30, 41, 59",
        "900": "15, 23, 42",
        "950": "2, 6, 23",
    },
    "indigo": {
        "50": "238, 242, 255",
        "100": "224, 231, 255",
        "200": "199, 210, 254",
        "300": "165, 180, 252",
        "400": "129, 140, 248",
        "500": "99, 102, 241",
        "600": "79, 70, 229",
        "700": "67, 56, 202",
        "800": "55, 48, 163",
        "900": "49, 46, 129",
        "950": "30, 27, 75",
    },
    "emerald": {
        "50": "236, 253, 245",
        "100": "209, 250, 229",
        "200": "167, 243, 208",
        "300": "110, 231, 183",
        "400": "52, 211, 153",
        "500": "16, 185, 129",
        "600": "5, 150, 105",
        "700": "4, 120, 87",
        "800": "6, 95, 70",
        "900": "6, 78, 59",
        "950": "2, 44, 34",
    },
    "rose": {
        "50": "255, 241, 242",
        "100": "255, 228, 230",
        "200": "254, 205, 211",
        "300": "253, 164, 175",
        "400": "251, 113, 133",
        "500": "244, 63, 94",
        "600": "225, 29, 72",
        "700": "190, 18, 60",
        "800": "159, 18, 57",
        "900": "136, 19, 55",
        "950": "76, 5, 25",
    },
    "amber": {
        "50": "255, 251, 235",
        "100": "254, 243, 199",
        "200": "253, 230, 138",
        "300": "252, 211, 77",
        "400": "251, 191, 36",
        "500": "245, 158, 11",
        "600": "217, 119, 6",
        "700": "180, 83, 9",
        "800": "146, 64, 14",
        "900": "120, 53, 15",
        "950": "69, 26, 3",
    },
    "cyan": {
        "50": "236, 254, 255",
        "100": "207, 250, 254",
        "200": "165, 243, 252",
        "300": "103, 232, 249",
        "400": "34, 211, 238",
        "500": "6, 182, 212",
        "600": "8, 145, 178",
        "700": "14, 116, 144",
        "800": "21, 94, 117",
        "900": "22, 78, 99",
        "950": "8, 51, 68",
    },
    "blue": {
        "50": "239, 246, 255",
        "100": "219, 234, 254",
        "200": "191, 219, 254",
        "300": "147, 197, 253",
        "400": "96, 165, 250",
        "500": "59, 130, 246",
        "600": "37, 99, 235",
        "700": "29, 78, 216",
        "800": "30, 64, 175",
        "900": "30, 58, 138",
        "950": "23, 37, 84",
    },
    "sky": {
        "400": "56, 189, 248",
        "500": "14, 165, 233",
    },
    "purple": {
        "500": "168, 85, 247",
        "950": "59, 7, 100",
    },
    "red": {
        "500": "239, 68, 68",
        "900": "127, 29, 29",
        "950": "69, 10, 10",
    },
    "brand": {
        "50": "238, 242, 255",
        "500": "99, 102, 241",
        "600": "79, 70, 229",
        "700": "67, 56, 202",
    },
    "black": {"DEFAULT": "0, 0, 0"},
    "white": {"DEFAULT": "255, 255, 255"},
}

def escape_selector(cls_name: str) -> str:
    """Escapes CSS selector characters like :, /, ., [, ]"""
    escaped = cls_name
    for char in [":", "/", ".", "[", "]"]:
        escaped = escaped.replace(char, "\\" + char)
    return escaped

def build_stylesheet() -> str:
    sections = []

    # 1. Reset, CSS Variables, and Base Theme
    sections.append("""/* DarkHub Self-Contained Stylesheet — Ticket DH-13 / USR-49 */
/* Eliminates external CDN dependencies with full local styling and modern Dark theme */

*, *::before, *::after {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

:root {
  --font-sans: 'Inter', system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, 'Open Sans', 'Helvetica Neue', sans-serif;
  --font-mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;
  color-scheme: dark;
}

html {
  font-family: var(--font-sans);
  background-color: #020617;
  color: #f8fafc;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

body {
  min-height: 100vh;
  background-color: #020617;
  color: #f8fafc;
}

::selection {
  background-color: #6366f1;
  color: #ffffff;
}

.selection\\:bg-indigo-500::selection {
  background-color: #6366f1;
}

.selection\\:text-white::selection {
  color: #ffffff;
}

.antialiased {
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

.dark, html.dark {
  color-scheme: dark;
}

.refresh-icon {
  display: inline-block;
  transition: transform 0.3s ease;
}

.refresh-icon.spinning {
  animation: spin 1s linear infinite;
}
""")

    # 2. Layout, Display & Positioning
    sections.append("""/* Layout, Display & Positioning */
.block { display: block; }
.inline-block { display: inline-block; }
.inline { display: inline; }
.flex { display: flex; }
.inline-flex { display: inline-flex; }
.grid { display: grid; }
.hidden { display: none; }

.relative { position: relative; }
.absolute { position: absolute; }
.fixed { position: fixed; }
.sticky { position: sticky; }
.static { position: static; }

.inset-0 { inset: 0; }
.inset-y-0 { top: 0; bottom: 0; }
.inset-x-0 { left: 0; right: 0; }

.top-0 { top: 0; }
.top-1 { top: 0.25rem; }
.top-2 { top: 0.5rem; }
.top-2\\.5 { top: 0.625rem; }
.top-full { top: 100%; }
.top-1\\/4 { top: 25%; }
.top-1\\/2 { top: 50%; }

.bottom-0 { bottom: 0; }
.bottom-2 { bottom: 0.5rem; }
.bottom-4 { bottom: 1rem; }
.bottom-6 { bottom: 1.5rem; }

.left-0 { left: 0; }
.left-2 { left: 0.5rem; }
.left-1\\/4 { left: 25%; }
.left-1\\/2 { left: 50%; }

.right-0 { right: 0; }
.right-2 { right: 0.5rem; }
.right-3 { right: 0.75rem; }
.right-4 { right: 1rem; }
.right-6 { right: 1.5rem; }
.right-1\\/4 { right: 25%; }

.-z-10 { z-index: -10; }
.z-0 { z-index: 0; }
.z-10 { z-index: 10; }
.z-20 { z-index: 20; }
.z-30 { z-index: 30; }
.z-40 { z-index: 40; }
.z-50 { z-index: 50; }

.pointer-events-none { pointer-events: none; }
.pointer-events-auto { pointer-events: auto; }
.select-none { user-select: none; }
.select-text { user-select: text; }
.cursor-pointer { cursor: pointer; }
.cursor-not-allowed { cursor: not-allowed; }
.cursor-default { cursor: default; }

.overflow-hidden { overflow: hidden; }
.overflow-visible { overflow: visible; }
.overflow-auto { overflow: auto; }
.overflow-x-auto { overflow-x: auto; }
.overflow-y-auto { overflow-y: auto; }
""")

    # 3. Flexbox & Grid Utilities
    sections.append("""/* Flexbox & Grid */
.flex-row { flex-direction: row; }
.flex-col { flex-direction: column; }
.flex-wrap { flex-wrap: wrap; }
.flex-nowrap { flex-wrap: nowrap; }
.flex-1 { flex: 1 1 0%; }
.flex-auto { flex: 1 1 auto; }
.flex-initial { flex: 0 1 auto; }
.flex-none { flex: none; }
.flex-shrink-0, .shrink-0 { flex-shrink: 0; }
.flex-grow { flex-grow: 1; }

.items-start { align-items: flex-start; }
.items-center { align-items: center; }
.items-end { align-items: flex-end; }
.items-baseline { align-items: baseline; }
.items-stretch { align-items: stretch; }

.justify-start { justify-content: flex-start; }
.justify-center { justify-content: center; }
.justify-end { justify-content: flex-end; }
.justify-between { justify-content: space-between; }
.justify-around { justify-content: space-around; }

.self-start { align-self: flex-start; }
.self-center { align-self: center; }
.self-end { align-self: flex-end; }
.self-auto { align-self: auto; }

.gap-0 { gap: 0; }
.gap-0\\.5 { gap: 0.125rem; }
.gap-1 { gap: 0.25rem; }
.gap-1\\.5 { gap: 0.375rem; }
.gap-2 { gap: 0.5rem; }
.gap-2\\.5 { gap: 0.625rem; }
.gap-3 { gap: 0.75rem; }
.gap-3\\.5 { gap: 0.875rem; }
.gap-4 { gap: 1rem; }
.gap-5 { gap: 1.25rem; }
.gap-6 { gap: 1.5rem; }
.gap-8 { gap: 2rem; }

.grid-cols-1 { grid-template-columns: repeat(1, minmax(0, 1fr)); }
.grid-cols-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.grid-cols-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.grid-cols-4 { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.grid-cols-5 { grid-template-columns: repeat(5, minmax(0, 1fr)); }
.grid-cols-6 { grid-template-columns: repeat(6, minmax(0, 1fr)); }
.grid-cols-12 { grid-template-columns: repeat(12, minmax(0, 1fr)); }
.grid-cols-\\[minmax\\(0\\,1fr\\)\\_auto\\] { grid-template-columns: minmax(0, 1fr) auto; }

.gap-x-3 { column-gap: 0.75rem; }
.gap-y-1 { row-gap: 0.25rem; }

.col-span-1 { grid-column: span 1 / span 1; }
.col-span-2 { grid-column: span 2 / span 2; }
.col-span-3 { grid-column: span 3 / span 3; }
.col-span-4 { grid-column: span 4 / span 4; }
.col-span-5 { grid-column: span 5 / span 5; }
.col-span-6 { grid-column: span 6 / span 6; }
.col-span-7 { grid-column: span 7 / span 7; }
.col-span-8 { grid-column: span 8 / span 8; }
.col-span-12 { grid-column: span 12 / span 12; }
.col-span-full { grid-column: 1 / -1; }
""")

    # 4. Spacing (Paddings, Margins, Spaces)
    spacing_map = {
        "0": "0px",
        "0.2": "0.05rem",
        "0.5": "0.125rem",
        "1": "0.25rem",
        "1.5": "0.375rem",
        "2": "0.5rem",
        "2.5": "0.625rem",
        "3": "0.75rem",
        "3.5": "0.875rem",
        "4": "1rem",
        "5": "1.25rem",
        "6": "1.5rem",
        "7": "1.75rem",
        "8": "2rem",
        "10": "2.5rem",
        "12": "3rem",
        "16": "4rem",
        "20": "5rem",
        "24": "6rem",
    }

    spacing_lines = ["/* Spacing: Margins, Paddings & Spaces */"]
    for k, val in spacing_map.items():
        escaped_k = k.replace(".", "\\.")
        spacing_lines.append(f".p-{escaped_k} {{ padding: {val}; }}")
        spacing_lines.append(f".px-{escaped_k} {{ padding-left: {val}; padding-right: {val}; }}")
        spacing_lines.append(f".py-{escaped_k} {{ padding-top: {val}; padding-bottom: {val}; }}")
        spacing_lines.append(f".pt-{escaped_k} {{ padding-top: {val}; }}")
        spacing_lines.append(f".pb-{escaped_k} {{ padding-bottom: {val}; }}")
        spacing_lines.append(f".pl-{escaped_k} {{ padding-left: {val}; }}")
        spacing_lines.append(f".pr-{escaped_k} {{ padding-right: {val}; }}")

        spacing_lines.append(f".m-{escaped_k} {{ margin: {val}; }}")
        spacing_lines.append(f".mx-{escaped_k} {{ margin-left: {val}; margin-right: {val}; }}")
        spacing_lines.append(f".my-{escaped_k} {{ margin-top: {val}; margin-bottom: {val}; }}")
        spacing_lines.append(f".mt-{escaped_k} {{ margin-top: {val}; }}")
        spacing_lines.append(f".mb-{escaped_k} {{ margin-bottom: {val}; }}")
        spacing_lines.append(f".ml-{escaped_k} {{ margin-left: {val}; }}")
        spacing_lines.append(f".mr-{escaped_k} {{ margin-right: {val}; }}")

        spacing_lines.append(f".space-y-{escaped_k} > :not([hidden]) ~ :not([hidden]) {{ margin-top: {val}; }}")
        spacing_lines.append(f".space-x-{escaped_k} > :not([hidden]) ~ :not([hidden]) {{ margin-left: {val}; }}")

    spacing_lines.append(".mx-auto { margin-left: auto; margin-right: auto; }")
    spacing_lines.append(".-mt-1 { margin-top: -0.25rem; }")
    spacing_lines.append(".-mt-2 { margin-top: -0.5rem; }")
    spacing_lines.append(".last\\:pb-0:last-child { padding-bottom: 0; }")
    sections.append("\n".join(spacing_lines) + "\n")

    # 5. Sizing (Width, Height, Max/Min)
    sections.append("""/* Sizing */
.w-auto { width: auto; }
.w-full { width: 100%; }
.w-screen { width: 100vw; }
.w-1 { width: 0.25rem; }
.w-1\\.5 { width: 0.375rem; }
.w-2 { width: 0.5rem; }
.w-2\\.5 { width: 0.625rem; }
.w-3 { width: 0.75rem; }
.w-3\\.5 { width: 0.875rem; }
.w-4 { width: 1rem; }
.w-5 { width: 1.25rem; }
.w-6 { width: 1.5rem; }
.w-7 { width: 1.75rem; }
.w-8 { width: 2rem; }
.w-9 { width: 2.25rem; }
.w-10 { width: 2.5rem; }
.w-12 { width: 3rem; }
.w-14 { width: 3.5rem; }
.w-16 { width: 4rem; }
.w-20 { width: 5rem; }
.w-24 { width: 6rem; }
.w-28 { width: 7rem; }
.w-32 { width: 8rem; }
.w-36 { width: 9rem; }
.w-40 { width: 10rem; }
.w-48 { width: 12rem; }
.w-64 { width: 16rem; }
.w-96 { width: 24rem; }
.w-1\\/2 { width: 50%; }
.w-2\\/3 { width: 66.666667%; }

.h-auto { height: auto; }
.h-full { height: 100%; }
.h-screen { height: 100vh; }
.h-1 { height: 0.25rem; }
.h-1\\.5 { height: 0.375rem; }
.h-2 { height: 0.5rem; }
.h-2\\.5 { height: 0.625rem; }
.h-3 { height: 0.75rem; }
.h-3\\.5 { height: 0.875rem; }
.h-4 { height: 1rem; }
.h-5 { height: 1.25rem; }
.h-6 { height: 1.5rem; }
.h-7 { height: 1.75rem; }
.h-8 { height: 2rem; }
.h-9 { height: 2.25rem; }
.h-10 { height: 2.5rem; }
.h-12 { height: 3rem; }
.h-14 { height: 3.5rem; }
.h-16 { height: 4rem; }
.h-20 { height: 5rem; }
.h-24 { height: 6rem; }
.h-28 { height: 7rem; }
.h-32 { height: 8rem; }
.h-48 { height: 12rem; }
.h-64 { height: 16rem; }
.h-96 { height: 24rem; }
.h-\\[2px\\] { height: 2px; }

.min-w-0 { min-width: 0; }
.min-w-full { min-width: 100%; }
.min-w-\\[180px\\] { min-width: 180px; }
.min-w-\\[240px\\] { min-width: 240px; }
.min-w-\\[260px\\] { min-width: 260px; }
.min-w-\\[320px\\] { min-width: 320px; }

.max-w-none { max-width: none; }
.max-w-xs { max-width: 20rem; }
.max-w-sm { max-width: 24rem; }
.max-w-md { max-width: 28rem; }
.max-w-lg { max-width: 32rem; }
.max-w-xl { max-width: 36rem; }
.max-w-2xl { max-width: 42rem; }
.max-w-3xl { max-width: 48rem; }
.max-w-4xl { max-width: 56rem; }
.max-w-5xl { max-width: 64rem; }
.max-w-6xl { max-width: 72rem; }
.max-w-7xl { max-width: 80rem; }
.max-w-full { max-width: 100%; }
.max-w-\\[120px\\] { max-width: 120px; }
.max-w-\\[140px\\] { max-width: 140px; }
.max-w-\\[200px\\] { max-width: 200px; }
.max-w-\\[220px\\] { max-width: 220px; }
.max-w-\\[1600px\\] { max-width: 1600px; }
.max-w-\\[1700px\\] { max-width: 1700px; }

.min-h-0 { min-height: 0; }
.min-h-screen { min-height: 100vh; }
.max-h-24 { max-height: 6rem; }
.max-h-32 { max-height: 8rem; }
.max-h-48 { max-height: 12rem; }
.max-h-60 { max-height: 15rem; }
.max-h-64 { max-height: 16rem; }
.max-h-80 { max-height: 20rem; }
.max-h-96 { max-height: 24rem; }
.max-h-\\[75vh\\] { max-height: 75vh; }
.max-h-\\[85vh\\] { max-height: 85vh; }
.max-h-\\[90vh\\] { max-height: 90vh; }
.max-h-\\[400px\\] { max-height: 400px; }
.max-h-\\[500px\\] { max-height: 500px; }
""")

    # 6. Typography
    sections.append("""/* Typography */
.font-sans { font-family: var(--font-sans); }
.font-mono { font-family: var(--font-mono); }

.font-light { font-weight: 300; }
.font-normal { font-weight: 400; }
.font-medium { font-weight: 500; }
.font-semibold { font-weight: 600; }
.font-bold { font-weight: 700; }

.text-\\[9px\\] { font-size: 9px; line-height: 12px; }
.text-\\[10px\\] { font-size: 10px; line-height: 14px; }
.text-\\[11px\\] { font-size: 11px; line-height: 15px; }
.text-\\[12px\\] { font-size: 12px; line-height: 16px; }
.text-\\[13px\\] { font-size: 13px; line-height: 18px; }
.text-\\[0\\.65rem\\] { font-size: 0.65rem; }
.text-\\[0\\.7rem\\] { font-size: 0.7rem; }
.text-\\[0\\.8rem\\] { font-size: 0.8rem; }

.text-xs { font-size: 0.75rem; line-height: 1rem; }
.text-sm { font-size: 0.875rem; line-height: 1.25rem; }
.text-base { font-size: 1rem; line-height: 1.5rem; }
.text-lg { font-size: 1.125rem; line-height: 1.75rem; }
.text-xl { font-size: 1.25rem; line-height: 1.75rem; }
.text-2xl { font-size: 1.5rem; line-height: 2rem; }
.text-3xl { font-size: 1.875rem; line-height: 2.25rem; }

.text-left { text-align: left; }
.text-center { text-align: center; }
.text-right { text-align: right; }

.uppercase { text-transform: uppercase; }
.lowercase { text-transform: lowercase; }
.capitalize { text-transform: capitalize; }
.normal-case { text-transform: none; }

.tracking-tighter { letter-spacing: -0.05em; }
.tracking-tight { letter-spacing: -0.025em; }
.tracking-normal { letter-spacing: 0em; }
.tracking-wide { letter-spacing: 0.025em; }
.tracking-wider { letter-spacing: 0.05em; }
.tracking-widest { letter-spacing: 0.1em; }
.tracking-\\[0\\.18em\\] { letter-spacing: 0.18em; }

.leading-none { line-height: 1; }
.leading-tight { line-height: 1.25; }
.leading-snug { line-height: 1.375; }
.leading-normal { line-height: 1.5; }
.leading-relaxed { line-height: 1.625; }
.leading-loose { line-height: 2; }
.leading-4 { line-height: 1rem; }
.leading-5 { line-height: 1.25rem; }
.leading-6 { line-height: 1.5rem; }

.truncate {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.whitespace-nowrap { white-space: nowrap; }
.whitespace-pre-wrap { white-space: pre-wrap; }

.underline { text-decoration-line: underline; }
.underline-offset-2 { text-underline-offset: 2px; }
.hover\\:underline:hover { text-decoration-line: underline; }

.line-clamp-2 {
  overflow: hidden;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.list-disc { list-style-type: disc; }
.list-inside { list-style-position: inside; }
""")

    # 7. Borders & Border Radius
    sections.append("""/* Borders & Radii */
.border-0 { border-width: 0px; }
.border { border-width: 1px; border-style: solid; }
.border-2 { border-width: 2px; border-style: solid; }
.border-t { border-top-width: 1px; border-top-style: solid; }
.border-b { border-bottom-width: 1px; border-bottom-style: solid; }
.border-l { border-left-width: 1px; border-left-style: solid; }
.border-r { border-right-width: 1px; border-right-style: solid; }
.border-x { border-left-width: 1px; border-right-width: 1px; border-style: solid; }
.border-y { border-top-width: 1px; border-bottom-width: 1px; border-style: solid; }
.border-collapse { border-collapse: collapse; }
.border-t-0 { border-top-width: 0px; }
.border-b-0 { border-bottom-width: 0px; }
.border-dashed { border-style: dashed; }
.border-solid { border-style: solid; }

.last\\:border-0:last-child { border-width: 0; }
.last\\:border-b-0:last-child { border-bottom-width: 0; }

.rounded-none { border-radius: 0px; }
.rounded-sm { border-radius: 0.125rem; }
.rounded { border-radius: 0.25rem; }
.rounded-md { border-radius: 0.375rem; }
.rounded-lg { border-radius: 0.5rem; }
.rounded-xl { border-radius: 0.75rem; }
.rounded-2xl { border-radius: 1rem; }
.rounded-3xl { border-radius: 1.5rem; }
.rounded-full { border-radius: 9999px; }

.rounded-t-none { border-top-left-radius: 0; border-top-right-radius: 0; }
.rounded-t-xl { border-top-left-radius: 0.75rem; border-top-right-radius: 0.75rem; }
.rounded-b-xl { border-bottom-left-radius: 0.75rem; border-bottom-right-radius: 0.75rem; }
""")

    # 8. Colors: Backgrounds, Text, Borders, Gradients & States
    color_lines = ["/* Colors: Background, Text, Border & Gradients */"]

    opacities = ["", "5", "10", "15", "20", "25", "30", "35", "40", "50", "55", "60", "65", "70", "75", "80", "90", "95"]

    for pal_name, pal_shades in PALETTES.items():
        for shade, rgb in pal_shades.items():
            base_name = f"{pal_name}-{shade}" if shade != "DEFAULT" else pal_name

            for op in opacities:
                if op == "":
                    # Pure color
                    css_color = f"rgb({rgb})"
                    suffix = ""
                    escaped_suffix = ""
                else:
                    alpha = round(int(op) / 100.0, 2)
                    css_color = f"rgba({rgb}, {alpha})"
                    suffix = f"/{op}"
                    escaped_suffix = f"\\/{op}"

                cls_bg = f"bg-{base_name}{escaped_suffix}"
                cls_text = f"text-{base_name}{escaped_suffix}"
                cls_border = f"border-{base_name}{escaped_suffix}"
                cls_divide = f"divide-{base_name}{escaped_suffix}"

                color_lines.append(f".{cls_bg} {{ background-color: {css_color}; }}")
                color_lines.append(f".{cls_text} {{ color: {css_color}; }}")
                color_lines.append(f".{cls_border} {{ border-color: {css_color}; }}")
                color_lines.append(f".{cls_divide} > :not([hidden]) ~ :not([hidden]) {{ border-color: {css_color}; }}")

                # Hover states
                color_lines.append(f".hover\\:{cls_bg}:hover {{ background-color: {css_color}; }}")
                color_lines.append(f".hover\\:{cls_text}:hover {{ color: {css_color}; }}")
                color_lines.append(f".hover\\:{cls_border}:hover {{ border-color: {css_color}; }}")

                # Focus states
                color_lines.append(f".focus\\:{cls_border}:focus {{ border-color: {css_color}; }}")
                color_lines.append(f".focus\\:{cls_bg}:focus {{ background-color: {css_color}; }}")

                # Gradients from/to
                if op == "":
                    color_lines.append(f".from-{base_name} {{ --tw-gradient-from: {css_color}; --tw-gradient-stops: var(--tw-gradient-from), var(--tw-gradient-to, rgba({rgb}, 0)); }}")
                    color_lines.append(f".to-{base_name} {{ --tw-gradient-to: {css_color}; }}")
                    color_lines.append(f".via-{base_name} {{ --tw-gradient-stops: var(--tw-gradient-from), {css_color}, var(--tw-gradient-to, rgba({rgb}, 0)); }}")

    # Common transparent / black / white extras
    color_lines.append(".bg-transparent { background-color: transparent; }")
    color_lines.append(".text-transparent { color: transparent; }")
    color_lines.append(".border-transparent { border-color: transparent; }")
    color_lines.append(".text-white { color: #ffffff; }")
    color_lines.append(".text-black { color: #000000; }")
    color_lines.append(".bg-white { background-color: #ffffff; }")
    color_lines.append(".bg-black { background-color: #000000; }")
    color_lines.append(".bg-black\\/10 { background-color: rgba(0, 0, 0, 0.1); }")
    color_lines.append(".bg-black\\/20 { background-color: rgba(0, 0, 0, 0.2); }")
    color_lines.append(".bg-black\\/30 { background-color: rgba(0, 0, 0, 0.3); }")
    color_lines.append(".bg-black\\/40 { background-color: rgba(0, 0, 0, 0.4); }")
    color_lines.append(".bg-black\\/50 { background-color: rgba(0, 0, 0, 0.5); }")

    # Additional specific color utilities found in JS templates
    color_lines.append(".text-purple-300 { color: #d8b4fe; }")
    color_lines.append(".text-purple-400 { color: #c084fc; }")
    color_lines.append(".text-red-200 { color: #fecaca; }")
    color_lines.append(".text-red-300 { color: #fca5a5; }")
    color_lines.append(".text-red-300\\/70 { color: rgba(252, 165, 165, 0.7); }")
    color_lines.append(".text-red-400 { color: #f87171; }")
    color_lines.append(".border-purple-800\\/40 { border-color: rgba(107, 33, 168, 0.4); }")
    color_lines.append(".border-red-800\\/50 { border-color: rgba(153, 27, 27, 0.5); }")
    color_lines.append(".border-sky-900\\/40 { border-color: rgba(12, 74, 110, 0.4); }")
    color_lines.append(".hover\\:bg-red-800\\/80:hover { background-color: rgba(153, 27, 27, 0.8); }")
    color_lines.append(".hover\\:text-purple-300:hover { color: #d8b4fe; }")
    color_lines.append(".hover\\:text-red-300:hover { color: #fca5a5; }")

    color_lines.append(".divide-y > :not([hidden]) ~ :not([hidden]) { border-top-width: 1px; border-bottom-width: 0; }")
    color_lines.append(".divide-x > :not([hidden]) ~ :not([hidden]) { border-left-width: 1px; border-right-width: 0; }")

    color_lines.append(".bg-gradient-to-r { background-image: linear-gradient(to right, var(--tw-gradient-stops)); }")
    color_lines.append(".bg-gradient-to-br { background-image: linear-gradient(to bottom right, var(--tw-gradient-stops)); }")
    color_lines.append(".bg-gradient-to-tr { background-image: linear-gradient(to top right, var(--tw-gradient-stops)); }")
    color_lines.append(".bg-gradient-to-b { background-image: linear-gradient(to bottom, var(--tw-gradient-stops)); }")
    color_lines.append(".bg-gradient-to-t { background-image: linear-gradient(to top, var(--tw-gradient-stops)); }")

    color_lines.append(".accent-indigo-500 { accent-color: #6366f1; }")
    color_lines.append(".accent-emerald-500 { accent-color: #10b981; }")

    color_lines.append(".placeholder-slate-400::placeholder { color: #94a3b8; }")
    color_lines.append(".placeholder-slate-500::placeholder { color: #64748b; }")
    color_lines.append(".placeholder-slate-600::placeholder { color: #475569; }")

    color_lines.append(".focus\\:ring-0:focus { box-shadow: none; outline: none; }")
    color_lines.append(".focus\\:outline-none:focus { outline: 2px solid transparent; outline-offset: 2px; }")
    color_lines.append(".focus-within\\:bg-slate-800\\/40:focus-within { background-color: rgba(30, 41, 59, 0.4); }")

    # Group hover states
    color_lines.append(".group:hover .group-hover\\:text-indigo-300 { color: #a5b4fc; }")
    color_lines.append(".group:hover .group-hover\\:text-indigo-400 { color: #818cf8; }")
    color_lines.append(".group:hover .group-hover\\:text-white { color: #ffffff; }")
    color_lines.append(".group:hover .group-hover\\:block { display: block; }")
    color_lines.append(".group\\/menu:hover .group-hover\\/menu\\:block { display: block; }")

    sections.append("\n".join(color_lines) + "\n")

    # 9. Effects, Shadows, Blurs & Opacity
    sections.append("""/* Shadows, Blurs & Opacity */
.shadow-sm { box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.05); }
.shadow { box-shadow: 0 1px 3px 0 rgba(0, 0, 0, 0.1), 0 1px 2px -1px rgba(0, 0, 0, 0.1); }
.shadow-md { box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -2px rgba(0, 0, 0, 0.1); }
.shadow-lg { box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -4px rgba(0, 0, 0, 0.1); }
.shadow-xl { box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 8px 10px -6px rgba(0, 0, 0, 0.1); }
.shadow-2xl { box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25); }
.shadow-inner { box-shadow: inset 0 2px 4px 0 rgba(0, 0, 0, 0.25); }

.shadow-indigo-500\\/25 { box-shadow: 0 10px 15px -3px rgba(99, 102, 241, 0.25), 0 4px 6px -4px rgba(99, 102, 241, 0.25); }
.shadow-indigo-600\\/20 { box-shadow: 0 10px 15px -3px rgba(79, 70, 229, 0.2); }
.shadow-indigo-600\\/30 { box-shadow: 0 10px 15px -3px rgba(79, 70, 229, 0.3); }
.shadow-indigo-950\\/10 { box-shadow: 0 10px 15px -3px rgba(30, 27, 75, 0.1); }
.shadow-emerald-500\\/10 { box-shadow: 0 10px 15px -3px rgba(16, 185, 129, 0.1); }
.shadow-emerald-500\\/20 { box-shadow: 0 10px 15px -3px rgba(16, 185, 129, 0.2), 0 4px 6px -4px rgba(16, 185, 129, 0.2); }
.shadow-emerald-600\\/30 { box-shadow: 0 10px 15px -3px rgba(5, 150, 105, 0.3); }
.shadow-emerald-950\\/10 { box-shadow: 0 10px 15px -3px rgba(2, 44, 34, 0.1); }
.shadow-amber-500\\/20 { box-shadow: 0 10px 15px -3px rgba(245, 158, 11, 0.2), 0 4px 6px -4px rgba(245, 158, 11, 0.2); }
.shadow-purple-500\\/10 { box-shadow: 0 10px 15px -3px rgba(168, 85, 247, 0.1); }
.shadow-sky-950\\/10 { box-shadow: 0 10px 15px -3px rgba(8, 47, 73, 0.1); }
.shadow-black\\/10 { box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1); }
.shadow-black\\/20 { box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.2); }
.shadow-black\\/40 { box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.4); }

.blur-sm { filter: blur(4px); }
.blur-md { filter: blur(12px); }
.blur-lg { filter: blur(16px); }
.blur-xl { filter: blur(24px); }
.blur-2xl { filter: blur(40px); }
.blur-3xl { filter: blur(64px); }

.backdrop-blur-sm { backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px); }
.backdrop-blur-md { backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); }
.backdrop-blur-lg { backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px); }
.backdrop-blur-xl { backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px); }

.opacity-0 { opacity: 0; }
.opacity-20 { opacity: 0.2; }
.opacity-40 { opacity: 0.4; }
.opacity-50 { opacity: 0.5; }
.opacity-60 { opacity: 0.6; }
.opacity-70 { opacity: 0.7; }
.opacity-75 { opacity: 0.75; }
.opacity-80 { opacity: 0.8; }
.opacity-90 { opacity: 0.9; }
.opacity-100 { opacity: 1; }

.disabled\\:opacity-40:disabled { opacity: 0.4; }
.disabled\\:pointer-events-none:disabled { pointer-events: none; }
""")

    # 10. Transitions, Transforms & Animations
    sections.append("""/* Transitions, Transforms & Animations */
.transition { transition-property: color, background-color, border-color, text-decoration-color, fill, stroke, opacity, box-shadow, transform, filter, backdrop-filter; transition-timing-function: cubic-bezier(0.4, 0, 0.2, 1); transition-duration: 150ms; }
.transition-all { transition-property: all; transition-timing-function: cubic-bezier(0.4, 0, 0.2, 1); transition-duration: 150ms; }
.transition-colors { transition-property: color, background-color, border-color, text-decoration-color, fill, stroke; transition-timing-function: cubic-bezier(0.4, 0, 0.2, 1); transition-duration: 150ms; }
.transition-opacity { transition-property: opacity; transition-timing-function: cubic-bezier(0.4, 0, 0.2, 1); transition-duration: 150ms; }
.transition-transform { transition-property: transform; transition-timing-function: cubic-bezier(0.4, 0, 0.2, 1); transition-duration: 150ms; }

.duration-75 { transition-duration: 75ms; }
.duration-100 { transition-duration: 100ms; }
.duration-150 { transition-duration: 150ms; }
.duration-200 { transition-duration: 200ms; }
.duration-300 { transition-duration: 300ms; }
.duration-500 { transition-duration: 500ms; }

.ease-linear { transition-timing-function: linear; }
.ease-in { transition-timing-function: cubic-bezier(0.4, 0, 1, 1); }
.ease-out { transition-timing-function: cubic-bezier(0, 0, 0.2, 1); }
.ease-in-out { transition-timing-function: cubic-bezier(0.4, 0, 0.2, 1); }

.transform { transform: translate(var(--tw-translate-x, 0), var(--tw-translate-y, 0)) rotate(var(--tw-rotate, 0)) skewX(var(--tw-skew-x, 0)) skewY(var(--tw-skew-y, 0)) scaleX(var(--tw-scale-x, 1)) scaleY(var(--tw-scale-y, 1)); }
.translate-x-full { --tw-translate-x: 100%; transform: translateX(100%); }
.translate-x-0 { --tw-translate-x: 0px; transform: translateX(0px); }
.-translate-x-full { --tw-translate-x: -100%; transform: translateX(-100%); }

.hover\\:scale-105:hover { transform: scale(1.05); }
.active\\:scale-95:active { transform: scale(0.95); }

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

@keyframes ping {
  75%, 100% {
    transform: scale(2);
    opacity: 0;
  }
}

@keyframes pulse {
  50% { opacity: .5; }
}

.animate-spin { animation: spin 1s linear infinite; }
.animate-ping { animation: ping 1s cubic-bezier(0, 0, 0.2, 1) infinite; }
.animate-pulse { animation: pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite; }

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border-width: 0;
}
.resize-y { resize: vertical; }
.resize-none { resize: none; }
""")

    # 11. Responsive Breakpoints (sm: 640px, md: 768px, lg: 1024px, xl: 1280px)
    sections.append("""/* Responsive Utilities */
@media (min-width: 640px) {
  .sm\\:block { display: block; }
  .sm\\:inline { display: inline; }
  .sm\\:inline-flex { display: inline-flex; }
  .sm\\:flex { display: flex; }
  .sm\\:grid { display: grid; }
  .sm\\:hidden { display: none; }

  .sm\\:flex-row { flex-direction: row; }
  .sm\\:items-center { align-items: center; }
  .sm\\:justify-between { justify-content: space-between; }
  .sm\\:w-auto { width: auto; }

  .sm\\:p-4 { padding: 1rem; }
  .sm\\:p-5 { padding: 1.25rem; }
  .sm\\:p-6 { padding: 1.5rem; }
  .sm\\:p-8 { padding: 2rem; }
  .sm\\:px-6 { padding-left: 1.5rem; padding-right: 1.5rem; }
  .sm\\:px-8 { padding-left: 2rem; padding-right: 2rem; }

  .sm\\:grid-cols-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .sm\\:grid-cols-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .sm\\:grid-cols-4 { grid-template-columns: repeat(4, minmax(0, 1fr)); }
}

@media (min-width: 768px) {
  .md\\:block { display: block; }
  .md\\:inline { display: inline; }
  .md\\:flex { display: flex; }
  .md\\:grid { display: grid; }
  .md\\:hidden { display: none; }

  .md\\:flex-row { flex-direction: row; }
  .md\\:items-center { align-items: center; }
  .md\\:self-auto { align-self: auto; }
  .md\\:pb-0 { padding-bottom: 0px; }

  .md\\:grid-cols-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .md\\:grid-cols-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .md\\:grid-cols-4 { grid-template-columns: repeat(4, minmax(0, 1fr)); }
}

@media (min-width: 1024px) {
  .lg\\:block { display: block; }
  .lg\\:inline { display: inline; }
  .lg\\:flex { display: flex; }
  .lg\\:grid { display: grid; }
  .lg\\:hidden { display: none; }

  .lg\\:flex-row { flex-direction: row; }
  .lg\\:items-center { align-items: center; }
  .lg\\:items-end { align-items: flex-end; }
  .lg\\:px-8 { padding-left: 2rem; padding-right: 2rem; }

  .lg\\:grid-cols-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .lg\\:grid-cols-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .lg\\:grid-cols-4 { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .lg\\:grid-cols-5 { grid-template-columns: repeat(5, minmax(0, 1fr)); }
  .lg\\:grid-cols-12 { grid-template-columns: repeat(12, minmax(0, 1fr)); }

  .lg\\:col-span-2 { grid-column: span 2 / span 2; }
  .lg\\:col-span-4 { grid-column: span 4 / span 4; }
  .lg\\:col-span-5 { grid-column: span 5 / span 5; }
  .lg\\:col-span-6 { grid-column: span 6 / span 6; }
  .lg\\:col-span-7 { grid-column: span 7 / span 7; }
  .lg\\:col-span-8 { grid-column: span 8 / span 8; }
  .lg\\:col-span-12 { grid-column: span 12 / span 12; }
}

@media (min-width: 1280px) {
  .xl\\:block { display: block; }
  .xl\\:flex { display: flex; }
  .xl\\:grid { display: grid; }

  .xl\\:flex-row { flex-direction: row; }
  .xl\\:items-center { align-items: center; }

  .xl\\:grid-cols-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .xl\\:grid-cols-4 { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .xl\\:grid-cols-6 { grid-template-columns: repeat(6, minmax(0, 1fr)); }

  .xl\\:col-span-2 { grid-column: span 2 / span 2; }
  .xl\\:col-span-3 { grid-column: span 3 / span 3; }
  .xl\\:col-span-4 { grid-column: span 4 / span 4; }
}
""")

    # 12. Preserved DarkHub Custom Styling (glassmorphism, scrollbars, status dots, roadmap, shimmer)
    sections.append("""/* ==========================================================================
   DarkHub Preserved Components: Glassmorphism, Roadmap, Custom Scrollbars
   ========================================================================== */

@keyframes pulseGlow {
  0%, 100% {
    box-shadow: 0 0 15px rgba(59, 130, 246, 0.2), inset 0 0 15px rgba(59, 130, 246, 0.05);
  }
  50% {
    box-shadow: 0 0 25px rgba(59, 130, 246, 0.4), inset 0 0 20px rgba(59, 130, 246, 0.1);
  }
}

@keyframes fadeInScale {
  from {
    opacity: 0;
    transform: scale(0.96) translateY(8px);
  }
  to {
    opacity: 1;
    transform: scale(1) translateY(0);
  }
}

.animate-in-quick {
  animation: fadeInScale 0.18s cubic-bezier(0.16, 1, 0.3, 1) forwards;
}

/* Glassmorphism cards */
.glass-panel {
  background: rgba(15, 23, 42, 0.75);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border: 1px solid rgba(255, 255, 255, 0.08);
}

.glass-card {
  background: linear-gradient(135deg, rgba(30, 41, 59, 0.65) 0%, rgba(15, 23, 42, 0.85) 100%);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid rgba(255, 255, 255, 0.07);
  transition: all 0.22s cubic-bezier(0.4, 0, 0.2, 1);
}

.glass-card:hover {
  transform: translateY(-3px);
  border-color: rgba(99, 102, 241, 0.35);
  box-shadow: 0 12px 28px -8px rgba(0, 0, 0, 0.5), 0 0 20px -3px rgba(99, 102, 241, 0.18);
}

/* Custom Scrollbars */
::-webkit-scrollbar {
  width: 7px;
  height: 7px;
}

::-webkit-scrollbar-track {
  background: rgba(15, 23, 42, 0.6);
}

::-webkit-scrollbar-thumb {
  background: rgba(71, 85, 105, 0.5);
  border-radius: 9999px;
}

::-webkit-scrollbar-thumb:hover {
  background: rgba(100, 116, 139, 0.8);
}

.scrollbar-none::-webkit-scrollbar {
  display: none;
}
.scrollbar-none {
  -ms-overflow-style: none;
  scrollbar-width: none;
}

/* Status Indicator Dots */
.status-dot-online {
  background-color: #10b981;
  box-shadow: 0 0 8px #10b981;
}

.status-dot-degraded {
  background-color: #f59e0b;
  box-shadow: 0 0 8px #f59e0b;
}

.status-dot-offline {
  background-color: #ef4444;
  box-shadow: 0 0 8px #ef4444;
}

.status-dot-unknown {
  background-color: #64748b;
}

/* Shimmer animation for loading states */
@keyframes shimmer {
  0% {
    background-position: -200% 0;
  }
  100% {
    background-position: 200% 0;
  }
}

.loading-shimmer {
  background: linear-gradient(90deg, rgba(255, 255, 255, 0.03) 25%, rgba(255, 255, 255, 0.08) 50%, rgba(255, 255, 255, 0.03) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
}

/* Operational roadmap */
.roadmap-stat {
  min-height: 90px;
  padding: 0.85rem 1rem;
  border: 1px solid rgba(51, 65, 85, 0.8);
  border-radius: 0.9rem;
  background: rgba(15, 23, 42, 0.65);
}

.roadmap-stat-label,
.roadmap-stat-note {
  display: block;
  color: #94a3b8;
  font-family: var(--font-mono);
  font-size: 0.65rem;
}

.roadmap-stat strong {
  display: block;
  margin: 0.25rem 0;
  color: #f8fafc;
  font-size: 1.25rem;
}

.roadmap-stat-note {
  color: #64748b;
  font-size: 0.58rem;
}

.roadmap-card {
  border: 1px solid rgba(51, 65, 85, 0.85);
  border-radius: 1rem;
  background: linear-gradient(135deg, rgba(30, 41, 59, 0.72), rgba(15, 23, 42, 0.9));
  transition: border-color 0.2s ease, transform 0.2s ease, box-shadow 0.2s ease;
}

.roadmap-card:hover,
.roadmap-card:focus-within {
  border-color: rgba(34, 211, 238, 0.55);
  box-shadow: 0 10px 24px -12px rgba(34, 211, 238, 0.4);
  transform: translateY(-2px);
}

.roadmap-card > button {
  padding: 1rem;
}

.roadmap-id {
  color: #67e8f9;
  font-family: var(--font-mono);
  font-size: 0.65rem;
  font-weight: 600;
}

.roadmap-list {
  display: grid;
  gap: 0.35rem;
}

.roadmap-list-item {
  border: 1px solid rgba(51, 65, 85, 0.75);
  border-radius: 0.7rem;
  background: rgba(15, 23, 42, 0.62);
  transition: border-color 0.2s ease, background 0.2s ease;
}

.roadmap-list-item:hover,
.roadmap-list-item:focus-within {
  border-color: rgba(34, 211, 238, 0.5);
  background: rgba(30, 41, 59, 0.78);
}

.roadmap-list-button {
  display: grid;
  width: 100%;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 0.75rem;
  padding: 0.65rem 0.8rem;
  text-align: left;
}

.roadmap-list-status {
  color: #67e8f9;
  font-family: var(--font-mono);
  font-size: 1rem;
  font-weight: 700;
}

.roadmap-list-main,
.roadmap-list-heading,
.roadmap-list-meta {
  display: flex;
  min-width: 0;
  align-items: center;
}

.roadmap-list-main {
  flex-direction: column;
  align-items: flex-start;
  gap: 0.25rem;
}

.roadmap-list-heading {
  width: 100%;
  gap: 0.6rem;
}

.roadmap-list-title {
  overflow: hidden;
  color: #e2e8f0;
  font-size: 0.78rem;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.roadmap-list-meta {
  flex-wrap: wrap;
  gap: 0.55rem;
  color: #64748b;
  font-family: var(--font-mono);
  font-size: 0.58rem;
}

.roadmap-list-tail {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 0.35rem;
  color: #64748b;
  font-family: var(--font-mono);
  font-size: 0.58rem;
  text-align: right;
}

.roadmap-list-count {
  white-space: nowrap;
}

.roadmap-table-intro {
  margin-bottom: 0.6rem;
  color: #64748b;
  font-family: var(--font-mono);
  font-size: 0.65rem;
}

.roadmap-sort-button {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  color: #94a3b8;
  font: inherit;
  text-align: left;
}

.roadmap-sort-button:hover,
.roadmap-sort-button:focus-visible {
  color: #cffafe;
}

.roadmap-sort-indicator {
  color: #22d3ee;
  font-size: 0.8rem;
}

.roadmap-tag {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  border: 1px solid rgba(100, 116, 139, 0.45);
  border-radius: 999px;
  padding: 0.18rem 0.45rem;
  font-family: var(--font-mono);
  font-size: 0.6rem;
  line-height: 1;
}

.roadmap-tag-status {
  border-color: rgba(34, 211, 238, 0.35);
  color: #a5f3fc;
  background: rgba(8, 145, 178, 0.12);
}

.roadmap-tag-neutral {
  color: #94a3b8;
  background: rgba(51, 65, 85, 0.25);
}

.roadmap-tag-warning {
  border-color: rgba(251, 191, 36, 0.4);
  color: #fcd34d;
  background: rgba(146, 64, 14, 0.15);
}

.roadmap-state-symbol {
  color: #67e8f9;
  font-family: var(--font-mono);
  font-size: 1.2rem;
  font-weight: 700;
}

.roadmap-mode-button {
  border-radius: 0.65rem;
  padding: 0.45rem 0.8rem;
  color: #94a3b8;
  font-family: var(--font-mono);
  font-size: 0.7rem;
}

.roadmap-mode-button:hover,
.roadmap-mode-active {
  color: #cffafe;
  background: rgba(8, 145, 178, 0.25);
}

.roadmap-alert {
  border: 1px solid rgba(100, 116, 139, 0.45);
  border-radius: 0.8rem;
  padding: 0.65rem 0.8rem;
  font-family: var(--font-mono);
  font-size: 0.68rem;
}

.roadmap-alert-neutral { color: #94a3b8; background: rgba(51, 65, 85, 0.2); }
.roadmap-alert-success { color: #86efac; border-color: rgba(34, 197, 94, 0.35); background: rgba(20, 83, 45, 0.18); }
.roadmap-alert-warning { color: #fde68a; border-color: rgba(245, 158, 11, 0.4); background: rgba(120, 53, 15, 0.2); }
.roadmap-alert-error { color: #fda4af; border-color: rgba(244, 63, 94, 0.4); background: rgba(127, 29, 29, 0.2); }

.roadmap-empty-state {
  border: 1px dashed rgba(100, 116, 139, 0.5);
  border-radius: 1rem;
  padding: 2rem;
  color: #94a3b8;
  text-align: center;
  font-family: var(--font-mono);
  font-size: 0.75rem;
}

.roadmap-source-pill {
  border: 1px solid rgba(34, 211, 238, 0.25);
  border-radius: 999px;
  padding: 0.25rem 0.5rem;
  color: #94a3b8;
  background: rgba(8, 145, 178, 0.08);
}

.roadmap-detail-heading {
  margin-bottom: 0.45rem;
  color: #94a3b8;
  font-family: var(--font-mono);
  font-size: 0.68rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

.roadmap-detail-list {
  display: grid;
  gap: 0.4rem;
  padding-left: 1rem;
  color: #cbd5e1;
  font-size: 0.72rem;
  list-style: disc;
}

.roadmap-timeline-intro,
.roadmap-graph-intro {
  margin-bottom: 0.8rem;
  color: #64748b;
  font-family: var(--font-mono);
  font-size: 0.68rem;
}

.roadmap-timeline {
  display: grid;
  grid-template-columns: repeat(5, minmax(170px, 1fr));
  gap: 0.75rem;
  overflow-x: auto;
  padding-bottom: 0.4rem;
}

.roadmap-timeline-column {
  min-height: 190px;
  padding: 0.85rem;
  border: 1px solid rgba(51, 65, 85, 0.8);
  border-radius: 1rem;
  background: rgba(15, 23, 42, 0.55);
}

.roadmap-timeline-item {
  border-left: 2px solid rgba(34, 211, 238, 0.55);
  border-radius: 0.6rem;
  background: rgba(30, 41, 59, 0.65);
}

.roadmap-timeline-item:hover,
.roadmap-timeline-item:focus-within {
  background: rgba(30, 41, 59, 0.95);
}

.roadmap-timeline-item button {
  padding: 0.65rem;
}

.roadmap-graph-shell {
  max-width: 100%;
  overflow: auto;
  border: 1px solid rgba(51, 65, 85, 0.8);
  border-radius: 1rem;
  background: radial-gradient(circle at top, rgba(8, 145, 178, 0.08), rgba(15, 23, 42, 0.8) 55%);
}

.roadmap-graph {
  display: block;
  min-width: 1050px;
  min-height: 170px;
  padding: 0.75rem;
}

.roadmap-graph-edge {
  fill: none;
  stroke: #22d3ee;
  stroke-opacity: 0.42;
  stroke-width: 1.5;
}

.roadmap-graph-node {
  cursor: pointer;
  outline: none;
}

.roadmap-graph-node:focus rect,
.roadmap-graph-node:hover rect {
  stroke: #f8fafc;
  stroke-width: 2;
}

@media (max-width: 900px) {
  .roadmap-timeline { grid-template-columns: repeat(5, minmax(190px, 1fr)); }
}

@media (max-width: 640px) {
  .roadmap-list-button { grid-template-columns: auto minmax(0, 1fr); }
  .roadmap-list-tail { grid-column: 2; justify-content: flex-start; text-align: left; }
  .roadmap-list-heading { align-items: flex-start; flex-direction: column; gap: 0.15rem; }
}

@media (prefers-reduced-motion: reduce) {
  .roadmap-card { transition: none; }
  .roadmap-card:hover,
  .roadmap-card:focus-within { transform: none; }
  .roadmap-list-item { transition: none; }
}
""")

    return "\n".join(sections)

def main() -> None:
    css_content = build_stylesheet()
    out_file1 = FRONTEND_DIR / "styles.css"
    out_file1.write_text(css_content, encoding="utf-8")
    
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    out_file2 = STATIC_DIR / "styles.css"
    out_file2.write_text(css_content, encoding="utf-8")

    size_kb = len(css_content.encode("utf-8")) / 1024
    print(f"Generated styles.css: {size_kb:.2f} KB ({len(css_content.encode('utf-8'))} bytes)")
    print(f"Written to {out_file1} and {out_file2}")

if __name__ == "__main__":
    main()
