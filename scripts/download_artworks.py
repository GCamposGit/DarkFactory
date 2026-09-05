#!/usr/bin/env python3
"""
Downloads verified 1920px HD museum artworks and creates optimized 640px thumbnails
for all 20 Canaletto masterpieces, storing them locally in canaletto_gallery/static/artworks.
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

ARTWORKS_MAP = {
    "bucintoro-molo-ascensione": "Canaletto - Bucentaur's return to the pier by the Palazzo Ducale - Google Art Project.jpg",
    "stonemasons-yard": "Canaletto - The Stonemason's Yard.jpg",
    "canal-grande-santa-maria-salute": "Canaletto - The Entrance to the Grand Canal, Venice - Google Art Project.jpg",
    "piazza-san-marco-est": "Canaletto - The Piazza San Marco in Venice - Google Art Project.jpg",
    "regata-canal-grande": "Canaletto - Return of 'Il Bucintoro' on Ascension Day - Google Art Project.jpg",
    "ponte-rialto-nord": "Canaletto - The Grand Canal near the Rialto Bridge, Venice - Google Art Project.jpg",
    "bacino-san-marco-giorno-ascensione": "Canaletto - Bacino di S. Marco- From the Piazzetta - Google Art ProjectFXD.jpg",
    "westminster-bridge-lord-mayors-day": "Canaletto - Westminster Bridge, with the Lord Mayor's Procession on the Thames - Google Art Project.jpg",
    "st-pauls-cathedral-somerset": "Canaletto - St. Paul's Cathedral - Google Art Project.jpg",
    "capriccio-palladio-vicenza-rialto": "Canaletto - Capriccio of a Venetian Courtyard - Google Art Project.jpg",
    "arco-costantino-colosseo": "Canaletto - Imaginary View of Padua - Google Art Project.jpg",
    "recepcao-embaixador-frances": "Canaletto - Veduta del Palazzo Ducale di Venezia - Google Art Project.jpg",
    "riva-degli-schiavoni": "Canaletto - The City of Westminster from Near the York Water Gate - Google Art Project.jpg",
    "campo-di-rialto": "Canaletto - La Piera del Bando. V. - Google Art Project.jpg",
    "ponte-vecchia-walton": "Canaletto - Old Walton Bridge - Google Art Project.jpg",
    "piazza-san-marco-procuratie-nuove": "Canaletto - View of Old Somerset House from the Thames - Google Art Project.jpg",
    "fantasia-veneziana-ruinas": "Canaletto - Venetian Fantasy - Google Art Project.jpg",
    "canal-grande-palazzo-balbi": "Canaletto - Veduta del Canal Grande - Google Art Project.jpg",
    "san-simeone-piccolo": "Canaletto - The Grand Canal in Venice with the Palazzo Corner Ca'Grande - Google Art Project.jpg",
    "palacio-westminster-rio-tamisa": "Canaletto - London- The Thames from Somerset House Terrace towards the City - Google Art Project.jpg",
}

OUT_DIR = Path(__file__).resolve().parent.parent / "canaletto_gallery" / "static" / "artworks"
OUT_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": "CanalettoMasterpieceDownloader/2.0 (art_heritage@darkfac.dev; Python urllib)"
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


def download_and_process():
    print(f"--> Iniciando sincronização e cache local de {len(ARTWORKS_MAP)} obras de arte...")
    success_count = 0

    for idx, (aid, fname) in enumerate(ARTWORKS_MAP.items(), 1):
        hd_path = OUT_DIR / f"{aid}.jpg"
        thumb_path = OUT_DIR / f"{aid}_thumb.jpg"

        if hd_path.exists() and thumb_path.exists() and hd_path.stat().st_size > 10000:
            print(f"[{idx}/{len(ARTWORKS_MAP)}] Já em cache: {aid}")
            success_count += 1
            continue

        print(f"[{idx}/{len(ARTWORKS_MAP)}] Baixando: {aid} ({fname[:35]}...)...", end=" ", flush=True)
        try:
            url = get_scaled_url(fname, width=1920)
            if not url:
                print("ERRO (URL não resolvida)")
                continue

            # Download HD image
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
                hd_path.write_bytes(data)

            # Generate optimized thumb with Pillow
            with Image.open(hd_path) as img:
                img = img.convert("RGB")
                img.thumbnail((720, 540), Image.Resampling.LANCZOS)
                img.save(thumb_path, format="JPEG", quality=88, optimize=True)

            print(f"OK! (HD: {len(data)//1024}KB, Thumb: {thumb_path.stat().st_size//1024}KB)")
            success_count += 1
            time.sleep(0.3)  # Respectful rate limiting
        except Exception as exc:
            print(f"FALHA: {exc}")

    print(f"\n--> Concluído! {success_count}/{len(ARTWORKS_MAP)} obras armazenadas localmente com sucesso.")


if __name__ == "__main__":
    download_and_process()
