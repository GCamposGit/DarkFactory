"""Generated sidebar utilities remain reproducible and responsive."""

from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


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
