"""Bildschirmfotos der Projektseite aus einer laufenden Demo-Instanz (Playwright, Firefox).

    python docs/website/tools/screenshots.py http://127.0.0.1:8077 OUT_DIR

Die Instanz muss mit demo_seed.py befüllt sein (Konten anna, ben, lea). Ergebnis:
PNG-Dateien in OUT_DIR, je Bild hell und dunkel (Suffix -dark).
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE, OUT = sys.argv[1].rstrip("/"), Path(sys.argv[2])
PASSWORD = "demo-demo-demo"
DESKTOP = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}

# (Datei, Konto, Pfad, Ansicht); Chats werden an den Anfang gescrollt.
SHOTS = [
    ("chat-rag", "anna", "/c/{heizung}/", DESKTOP),
    ("chat-code", "ben", "/c/{csv}/", DESKTOP),
    ("chat-phone", "lea", "/c/{bio}/", PHONE),
    ("collections", "anna", "/sammlungen/{familie}/", DESKTOP),
    ("usage", "anna", "/verbrauch/", DESKTOP),
]


def ids():
    import subprocess

    rows = subprocess.run(
        [
            "psql",
            "-d",
            "multigpt_demo",
            "-Atc",
            "select 'heizung', id from chat_conversation where title like 'Heizung%' union all "
            "select 'csv', id from chat_conversation where title like 'CSV%' union all "
            "select 'bio', id from chat_conversation where title like 'Foto%' union all "
            "select 'familie', id from chat_collection where name = 'Familienordner'",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    return dict(row.split("|") for row in rows)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    keys = ids()
    with sync_playwright() as p:
        browser = p.firefox.launch()
        for scheme in ("light", "dark"):
            for name, account, path, viewport in SHOTS:
                ctx = browser.new_context(
                    viewport=viewport, color_scheme=scheme, locale="de-DE", device_scale_factor=1
                )
                page = ctx.new_page()
                page.goto(f"{BASE}/konto/login/")
                page.fill("input[name=username]", account)
                page.fill("input[name=password]", PASSWORD)
                page.click("button[type=submit], input[type=submit]")
                page.wait_for_load_state("networkidle")
                page.goto(BASE + path.format(**keys))
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(600)
                page.evaluate("document.querySelector('.chat-history')?.scrollTo(0, 0)")
                page.wait_for_timeout(200)
                suffix = "-dark" if scheme == "dark" else ""
                page.screenshot(path=str(OUT / f"{name}{suffix}.png"))
                ctx.close()
        browser.close()


main()
