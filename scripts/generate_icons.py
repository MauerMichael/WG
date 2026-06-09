"""Generiert die PWA-/Favicon-Icons im Brand-Stil ("Sanft & gemütlich").

Motiv: das App-Logo (violettes, abgerundetes Quadrat mit weißem Haus) als
gefüllte, gut lesbare Silhouette — weißes Haus + goldene Tür auf Brand-Violett.
Gefülltes statt dünn-umrandetes Haus, weil das auch bei 16 px noch klar liest.

Rendert mit 4-fachem Supersampling und skaliert per LANCZOS herunter -> knackige,
anti-aliaste Kanten. Idempotent: überschreibt die Dateien bei jedem Lauf.

    .\\venv\\Scripts\\python.exe .\\scripts\\generate_icons.py

Benötigt Pillow (in requirements-dev.txt). Die erzeugten PNGs werden eingecheckt;
das Skript läuft nur, wenn das Icon neu gebaut werden soll.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

# --- Brand-Farben (siehe tailwind.config.js) ---------------------------------
VIOLET = (124, 58, 237, 255)  # brand-600  #7c3aed
WHITE = (255, 255, 255, 255)
GOLD = (245, 197, 24, 255)  # gold-400   #f5c518
TRANSPARENT = (0, 0, 0, 0)

SS = 4  # Supersampling-Faktor
OUT_DIR = Path(__file__).resolve().parents[1] / "app" / "static" / "img" / "icons"


def _draw_house(draw: ImageDraw.ImageDraw, cx: float, cy: float, size: float) -> None:
    """Zeichnet das gefüllte Haus (weiß + goldene Tür), zentriert auf (cx, cy).

    Koordinaten leben auf einem 24x24-Raster (wie das Lucide-„home"-SVG) und
    werden auf eine Kantenlänge ``size`` skaliert.
    """
    s = size / 24.0

    def P(x: float, y: float) -> tuple[float, float]:
        return (cx + (x - 12.0) * s, cy + (y - 12.0) * s)

    # Dach (gefülltes Dreieck mit leichtem Überstand).
    draw.polygon([P(12, 2.5), P(2.8, 11.4), P(21.2, 11.4)], fill=WHITE)
    # Korpus (abgerundetes Rechteck, überlappt die Dachbasis -> nahtlos).
    draw.rounded_rectangle([P(4.9, 10.6), P(19.1, 21.0)], radius=1.7 * s, fill=WHITE)
    # Tür (goldenes, oben abgerundetes Rechteck, bündig mit der Unterkante).
    draw.rounded_rectangle([P(10.1, 14.2), P(13.9, 21.0)], radius=0.9 * s, fill=GOLD)


def _make_icon(px: int, *, maskable: bool, house_frac: float) -> Image.Image:
    """Baut ein einzelnes Icon der Kantenlänge ``px``.

    ``maskable``/apple -> randloses Violett (Plattform rundet selbst).
    Sonst -> abgerundetes Quadrat auf transparentem Grund.
    """
    big = px * SS
    img = Image.new("RGBA", (big, big), TRANSPARENT)
    draw = ImageDraw.Draw(img)

    if maskable:
        draw.rectangle([0, 0, big, big], fill=VIOLET)
    else:
        draw.rounded_rectangle([0, 0, big - 1, big - 1], radius=0.225 * big, fill=VIOLET)

    _draw_house(draw, big / 2, big / 2, house_frac * big)
    return img.resize((px, px), Image.LANCZOS)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Standard-Icons ("any"): abgerundetes Quadrat, transparente Ecken.
    for px in (192, 512):
        _make_icon(px, maskable=False, house_frac=0.58).save(OUT_DIR / f"icon-{px}.png")

    # Maskable: randloses Violett, Motiv in der zentralen Safe-Zone (~52 %).
    for px in (192, 512):
        _make_icon(px, maskable=True, house_frac=0.52).save(
            OUT_DIR / f"icon-maskable-{px}.png"
        )

    # Apple-Touch-Icon: randloses Quadrat (iOS rundet + maskiert selbst, mag kein Alpha).
    _make_icon(180, maskable=True, house_frac=0.58).save(OUT_DIR / "apple-touch-icon.png")

    # Favicons: abgerundetes Quadrat, etwas größeres Haus für Mini-Größen.
    _make_icon(32, maskable=False, house_frac=0.62).save(OUT_DIR / "favicon-32.png")
    _make_icon(16, maskable=False, house_frac=0.64).save(OUT_DIR / "favicon-16.png")

    # Multi-Size .ico (16/32/48) für klassische Browser-Tabs.
    ico = _make_icon(48, maskable=False, house_frac=0.62)
    ico.save(OUT_DIR / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])

    written = sorted(p.name for p in OUT_DIR.glob("*") if p.suffix in {".png", ".ico"})
    print(f"{len(written)} Icons geschrieben nach {OUT_DIR}:")
    for name in written:
        print(f"  - {name}")


if __name__ == "__main__":
    main()
