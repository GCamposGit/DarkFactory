"""Static contract for the DarkHub sidebar migration."""
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from lxml import html

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = [(None, 'line-live-link', 'Esteira ao vivo: tickets da fábrica etapa por etapa', 'Esteira ao vivo'),
 ('openBenchmarksModal()',
  None,
  'Ver Benchmarks & Fronteira de Pareto',
  '📊\n          Benchmarks & Pareto'),
 ('openLearningDrawer()',
  None,
  'Abrir Central de Aprendizado, Learning Packs & Segundo Cérebro',
  '🎓\n          Aprendizado'),
 ('openPortfolioDrawer()',
  'portfolio-trigger-btn',
  'Abrir Portfólio Multiprojeto (DH-08)',
  '💼\n          Portfólio'),
 ('openRoadmapDrawer()', None, 'Abrir Roadmap Operacional', '🗺️\n          Roadmap'),
 ('openDemandsDrawer()',
  None,
  'Abrir Central de Demandas do Usuário & Backlog',
  '📝\n          Demandas'),
 ('openTelemetryDrawer()',
  None,
  'Abrir Telemetria Estruturada de Modelos de IA',
  '📈\n          Telemetria'),
 ("document.getElementById('content-studio-section')?.scrollIntoView({behavior: 'smooth'})",
  None,
  'Estúdio de Conteúdo Anti-Slop & Ateliê Visual (DH-07)',
  '🎨\n          Estúdio'),
 ("document.getElementById('harness-validation-section')?.scrollIntoView({behavior: 'smooth'})",
  None,
  'Validação sob Demanda & Harness Remoto (DH-11)',
  '🧪\n          Testes'),
 ("document.getElementById('infrastructure-cards-section')?.scrollIntoView({behavior: 'smooth'})",
  None,
  'Recursos de Infraestrutura & Rede',
  '🖥️\n          Infra'),
 ("document.getElementById('tasks-dashboard-section')?.scrollIntoView({behavior: 'smooth'})",
  None,
  'Fila operacional de tarefas',
  '✅\n          Tarefas'),
 ('openPlaygroundDrawer()',
  None,
  'Abrir AI Playground (Local Ollama & OpenRouter)',
  '🧠\n          Playground'),
 ('openPromptVaultDrawer()', None, 'Abrir Cofre de Prompts', '📋\n          Prompts'),
 ('openBackupModal()', None, 'Backup & Configurações', '⚙️')]

def document():
    return html.fromstring((ROOT / "hub/frontend/index.html").read_text(encoding="utf-8"))

def test_sidebar_exists_with_single_nav_and_aria_label():
    sidebars = document().xpath('//aside[@id="hub-sidebar"]')
    assert len(sidebars) == 1
    assert len(sidebars[0].xpath('./nav[@aria-label]')) == 1
    assert {"fixed", "inset-y-0", "left-0", "w-64", "overflow-y-auto", "lg:translate-x-0"} <= set(sidebars[0].get('class').split())
    toggle = document().get_element_by_id('hub-sidebar-toggle')
    assert toggle.get('aria-controls') == 'hub-sidebar'
    assert toggle.get('aria-expanded') == 'false'
    assert 'lg:hidden' in toggle.get('class').split()
    assert 'hidden' in document().get_element_by_id('hub-sidebar-overlay').get('class').split()

def test_header_has_no_navigation_handlers():
    header = document().xpath('//header')[0]
    assert not header.xpath('.//a[@id="line-live-link"]')
    assert all(n.get('onclick') in ('openCoverageDrawer()', 'openCommandPalette()', 'openServiceModal()') for n in header.xpath('.//*[@onclick]'))

def test_all_original_nav_handlers_preserved_in_sidebar():
    nodes = document().get_element_by_id('hub-sidebar').xpath('.//button[@onclick] | .//a')
    actual = [(n.get('onclick'), n.get('id'), n.get('title'), ''.join(n.itertext()).strip()) for n in nodes]
    assert actual == EXPECTED
    assert nodes[0].get('href') == '/live'
    assert len(document().xpath('//*[@aria-current="page"]')) == 1

def test_topbar_keeps_brand_search_project_select_badges_and_new_button():
    header = document().xpath('//header')[0]
    assert 'DarkHub' in header.text_content()
    for identifier in ('hub-coverage-badge', 'ollama-status-badge', 'openrouter-status-badge', 'global-project-select'):
        assert header.xpath('.//*[@id=$identifier]', identifier=identifier)
    assert header.xpath('.//button[@onclick="openCommandPalette()"]//kbd')[0].text == 'Ctrl+K'
    assert header.xpath('.//button[@onclick="openServiceModal()"]')

def test_main_has_lg_ml_64_offset():
    assert 'lg:ml-64' in document().xpath('//main')[0].get('class').split()

def test_scripts_share_single_cache_version():
    versions = [parse_qs(urlsplit(n.get('src')).query).get('v', []) for n in document().xpath('//script[@src]')]
    assert versions and all(v and v == versions[0] and v[0] for v in versions)


def test_generate_hub_styles_is_idempotent_and_mirrors_match(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    frontend = tmp_path / "hub" / "frontend"
    frontend.mkdir(parents=True)
    generator = scripts / "generate_hub_styles.py"
    shutil.copyfile(ROOT / "scripts" / generator.name, generator)
    outputs = (frontend / "styles.css", frontend / "static" / "styles.css")
    subprocess.run([sys.executable, str(generator)], check=True, capture_output=True)
    first = tuple(path.read_bytes() for path in outputs)
    subprocess.run([sys.executable, str(generator)], check=True, capture_output=True)
    assert first == tuple(path.read_bytes() for path in outputs)
    assert first[0] == first[1]
    assert first == tuple((ROOT / path.relative_to(tmp_path)).read_bytes() for path in outputs)


def test_stylesheet_has_sidebar_utilities():
    css = (ROOT / "hub/frontend/styles.css").read_text(encoding="utf-8")
    for rule in (
        ".ml-64 { margin-left: 16rem; }",
        ".w-64 { width: 16rem; }",
        ".fixed { position: fixed; }",
        ".inset-y-0 { top: 0; bottom: 0; }",
        ".left-0 { left: 0; }",
        ".z-40 { z-index: 40; }",
        ".z-50 { z-index: 50; }",
        ".overflow-y-auto { overflow-y: auto; }",
        ".-translate-x-full { --tw-translate-x: -100%; transform: translateX(-100%); }",
        ".translate-x-0 { --tw-translate-x: 0px; transform: translateX(0px); }",
    ):
        assert rule in css
    desktop = re.search(r"@media \(min-width: 1024px\) \{(.*?)\n\}", css, re.S)
    assert desktop is not None
    for rule in (
        r".lg\:ml-64 { margin-left: 16rem; }",
        r".lg\:translate-x-0 { --tw-translate-x: 0px; transform: translateX(0px); }",
        r".lg\:static { position: static; }",
    ):
        assert rule in desktop.group(1)


def app_source():
    return (ROOT / "hub/frontend/app.js").read_text(encoding="utf-8")


def test_app_js_defines_sidebar_toggle_api():
    source = app_source()
    for name in ("openHubSidebar", "closeHubSidebar", "toggleHubSidebar", "setHubSidebarActive"):
        assert f"function {name}(" in source
    setup = source.split("function setupEventListeners()", 1)[1]
    assert 'addEventListener("click", toggleHubSidebar)' in setup
    assert 'setHubSidebarActive(item.dataset.sidebarId)' in setup
    assert source.count('window.addEventListener("keydown"') == 1


def test_sidebar_escape_and_focus_return_implemented():
    source = app_source()
    escape = source.split('e.key === "Escape"', 1)[1].split('// Search input', 1)[0]
    assert re.search(r'if .*aria-expanded.*=== "true".*closeHubSidebar\(\);.*else.*closeAllModals\(\);', escape, re.S)
    assert 'if (returnFocus && wasOpen && window.innerWidth < 1024) toggle.focus();' in source


def test_sidebar_toggle_syncs_aria_expanded_and_overlay():
    source = app_source()
    opening = source.split('function openHubSidebar()', 1)[1].split('function closeHubSidebar', 1)[0]
    closing = source.split('function closeHubSidebar', 1)[1].split('function toggleHubSidebar', 1)[0]
    assert 'if (window.innerWidth >= 1024) return;' in opening
    assert 'classList.remove("hidden")' in opening
    assert 'setAttribute("aria-expanded", "true")' in opening
    assert 'classList.add("hidden")' in closing
    assert 'setAttribute("aria-expanded", "false")' in closing
    assert '"hub-sidebar-overlay").addEventListener("click", () => closeHubSidebar())' in source
    assert 'closeHubSidebar(false);' in source.split('function setupEventListeners()', 1)[1]


def test_set_hub_sidebar_active_keeps_single_aria_current():
    active = app_source().split('function setHubSidebarActive(id)', 1)[1].split('function setupEventListeners', 1)[0]
    assert '[aria-current]' in active
    assert active.index('removeAttribute("aria-current")') < active.index('setAttribute("aria-current", "page")')
    assert '.find(' in active  # Select one item even if a malformed DOM repeats its identifier.


def test_sidebar_closes_after_item_selection_on_mobile():
    setup = app_source().split('function setupEventListeners()', 1)[1]
    click = setup.split('"hub-sidebar").addEventListener("click"', 1)[1].split('window.addEventListener', 1)[0]
    assert 'event.target.closest("[data-sidebar-id]")' in click
    assert 'if (window.innerWidth < 1024) closeHubSidebar();' in click
    assert '}, true);' in click  # Close before the original drawer handler moves focus.
    assert 'preventDefault' not in click and 'stopPropagation' not in click
