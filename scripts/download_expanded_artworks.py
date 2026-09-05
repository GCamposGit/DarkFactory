#!/usr/bin/env python3
"""
Downloads and generates 1920px HD and 640px thumbnails for the expanded 14 Canaletto artworks,
bringing the total collection to 34 museum-verified masterpieces.
"""

import os
import sys
import json
import time
import urllib.request
import urllib.parse
from pathlib import Path
from PIL import Image

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

NEW_ARTWORKS_MAP = {
    "warwick-castle-priory": "Canaletto - Warwick- The Town and Castle from the Priory Gardens - Google Art Project.jpg",
    "warwick-castle-south": "Canaletto - Warwick Castle - Google Art Project.jpg",
    "thames-somerset-westminster": "Canaletto - The Thames from the Terrace of Somerset House, Looking toward Westminster - Google Art Project.jpg",
    "thames-somerset-st-pauls": "Canaletto - The Thames from the Terrace of Somerset House, Looking toward St. Paul's - Google Art Project.jpg",
    "westminster-york-water-gate": "Canaletto - The City of Westminster from Near the York Water Gate - Google Art Project.jpg",
    "porta-portello-padua": "Giovanni Antonio Canal, il Canaletto - The Porta Portello with the Brenta Canal in Padua, 1740-1743 - Google Art Project.jpg",
    "prato-della-valle-padua": "Giovanni Antonio Canal called Il Canaletto - Prà della Valle in Padua - Google Art Project.jpg",
    "locks-at-dolo": "Canaletto - The Locks at Dolo - Google Art Project.jpg",
    "portico-lantern-capriccio": "Giovanni Antonio Canal, il Canaletto - The portico with a lantern - from the series 'Vedute' (Views) - Google Art Project.jpg",
    "capriccio-monumental-staircase": "Canaletto - A capriccio with a monumental staircase - Google Art Project.jpg",
    "grand-canal-palazzo-corner": "Canaletto - The Grand Canal in Venice with the Palazzo Corner Ca'Grande - Google Art Project.jpg",
    "grand-canal-rialto-bridge": "Canaletto - The Grand Canal near the Rialto Bridge, Venice - Google Art Project.jpg",
    "baroque-colonnade-garden": "Giovanni Antonio Canal, il Canaletto - View through a Baroque Colonnade into a Garden, 1760-1768 - Google Art Project.jpg",
    "old-walton-middlesex": "Canaletto - Old Walton Bridge seen from the Middlesex Shore - Google Art Project.jpg",
}

OUT_DIR = Path(__file__).resolve().parent.parent / "canaletto_gallery" / "static" / "artworks"
OUT_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": "CanalettoExpandedCollector/2.0 (art_conservation@darkfac.dev)"
}


def get_scaled_url(filename: str, width: int = 1920) -> str:
    params = {
        "action": "query",
        "format": "json",
        "titles": "File:" + filename,
        "prop": "imageinfo",
        "iiprop": "url",
        "iiurlwidth": str(width),
    }
    api_url = "https://commons.wikimedia.org/w/api.php?" + "&".join(
        f"{k}={urllib.parse.quote(v)}" for k, v in params.items()
    )
    req = urllib.request.Request(api_url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        pages = data.get("query", {}).get("pages", {})
        for pid, p in pages.items():
            if pid != "-1":
                ii = p.get("imageinfo", [{}])[0]
                return ii.get("thumburl") or ii.get("url")
    return ""


def main():
    print(f"--> Iniciando download das 14 novas obras de arte de Canaletto...")
    success = 0

    for idx, (aid, fname) in enumerate(NEW_ARTWORKS_MAP.items(), 1):
        hd_path = OUT_DIR / f"{aid}.jpg"
        thumb_path = OUT_DIR / f"{aid}_thumb.jpg"

        if hd_path.exists() and thumb_path.exists() and hd_path.stat().st_size > 10000:
            print(f"[{idx}/{len(NEW_ARTWORKS_MAP)}] Já em cache local: {aid}")
            success += 1
            continue

        print(f"[{idx}/{len(NEW_ARTWORKS_MAP)}] Baixando: {aid}...", end=" ", flush=True)
        try:
            url = get_scaled_url(fname, width=1920)
            if not url:
                print(f"FALHA (URL não encontrada para {fname})")
                continue

            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
                hd_path.write_bytes(data)

            with Image.open(hd_path) as img:
                img = img.convert("RGB")
                img.thumbnail((720, 540), Image.Resampling.LANCZOS)
                img.save(thumb_path, format="JPEG", quality=88, optimize=True)

            print(f"OK! (HD: {len(data)//1024}KB, Thumb: {thumb_path.stat().st_size//1024}KB)")
            success += 1
            time.sleep(0.3)
        except Exception as exc:
            print(f"ERRO: {exc}")

    print(f"\n--> Sucesso! {success}/{len(NEW_ARTWORKS_MAP)} novas obras prontas localmente.")


if __name__ == "__main__":
    main()
