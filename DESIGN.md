# Design-System — WG-Organise

> **Vibe: „Sanft & gemütlich".** Pastelliges Violett + warmes **Gold** auf weichem Lila-Weiß,
> großzügig runde Ecken, weiche Schatten, runder freundlicher Font (Varela Round für Überschriften,
> Nunito für Text). Leitprinzip: **übersichtlich, macht Spaß, vor allem unkompliziert.** Bei allem
> Neuen gilt: erst die vorhandenen Bausteine nutzen, nicht neue Ad-hoc-Styles erfinden.
> Mobile-first: untere Tab-Leiste auf dem Handy, Tap-Ziele ≥ 44 px, keine horizontal scrollenden Tabellen.

Alle Bausteine sind zentral definiert — **bitte hier nichts duplizieren, sondern wiederverwenden:**
- Farben/Font/Schatten → `tailwind.config.js`
- Komponenten-Klassen + Basis-Styles → `app/static/css/input.css`
- Layout, Navigation, Flash → `app/templates/base.html`
- Macros (Icons, Badges, Header, Empty-State) → `app/templates/components/`

---

## 1. Farben — wann welche?

| Token        | Farbe            | Wofür                                                            |
|--------------|------------------|-----------------------------------------------------------------|
| `brand-*`    | Violett          | **Marke + Primär-Aktion** (Hauptbuttons, aktive Nav, Akzente)   |
| `gold-*`     | Gold             | **Belohnung / positive Highlights** („Erledigt", Score, Ehrenpunkte) |
| `surface`    | Sanftes Lila-Weiß | **Seiten-Hintergrund** (`body`). Karten bleiben `white`.        |
| `slate-*`    | Grau             | **Text**: `900` stark · `600` sekundär · `500/400` gedämpft     |

**Semantik (Status):**
- `emerald` = erledigt/erfolgreich · `rose` = Gefahr/Löschen/abgelehnt
- `brand`/`sky` = Info/geplant · `gold` = Warnung/läuft gerade / Belohnung

Faustregel: **Violett führt, Gold belohnt, Weiß atmet.** Gold sparsam für die schönen Momente.
Das alte `accent-*` (Neon-Gelb) gibt es nicht mehr — überall `gold-*` nutzen.

---

## 2. Komponenten-Klassen (`@layer components` in `input.css`)

Statt Utilities zu wiederholen → diese semantischen Klassen nutzen:

```html
<!-- Buttons: immer .btn + genau eine Variante -->
<button class="btn btn-primary">Speichern</button>      <!-- Violett, Haupt-Aktion -->
<button class="btn btn-accent">Erledigt</button>        <!-- Gelb, positiv -->
<a class="btn btn-ghost">Abbrechen</a>                  <!-- dezent -->
<button class="btn btn-danger">Löschen</button>         <!-- rose -->
<button class="btn btn-primary btn-sm">Klein</button>   <!-- + btn-sm = kompakt -->

<!-- Karten -->
<div class="card">…</div>            <!-- weiß, rounded-2xl, shadow-soft, p-5 -->
<div class="card-muted">…</div>      <!-- sanfte Violett-Tönung -->

<!-- Badges / Pills -->
<span class="badge badge-success">erledigt</span>
<!-- Varianten: badge-success | badge-warning | badge-info | badge-neutral | badge-danger | badge-gold -->

<!-- Formulare -->
<div class="field">
  <label class="label">Titel</label>
  <input class="input" name="title">          <!-- .input geht auch auf select & textarea -->
  <p class="field-error">Pflichtfeld</p>
</div>

<!-- Sonstiges -->
<h1 class="page-title">Überschrift</h1>
<div class="empty-state">…</div>               <!-- Leerzustand -->

<!-- Sub-Navigation (Pill-Segmente innerhalb eines Bereichs, z.B. Verwaltung/Aufgaben) -->
<nav class="subnav"><a class="subnav-link subnav-link-active">…</a></nav>

<!-- Stat-Kachel (Kennzahlen) -->
<div class="stat-tile"><span class="stat-value text-gold-600">21</span><span class="stat-label">Punkte</span></div>

<!-- Tabelle-als-Karten (responsiv statt horizontalem Scrollen): pro Eintrag eine Karte -->
<div class="data-card"><div class="kv"><span class="kv-label">Status</span><span>…</span></div></div>

<!-- Aktions-Gruppe: mehrere Buttons/Formulare; .btn-block = voll-breit auf Handy, inline ab sm -->
<div class="action-group"><button class="btn btn-primary btn-sm btn-block">…</button></div>

<!-- Kalender: Tages-Karte + Tag-Chip (≥44px) -->
<div class="cal-day cal-day-today"><div class="cal-head">…</div>…</div>
```

Navigation (`nav-link`/`nav-link-active`, `tab`/`tab-active`) wird **nur** in `base.html` benutzt.
Die **Sub-Navigation** (`subnav`/`subnav-link`/`subnav-link-active`) steht dagegen oben im Content
eines Bereichs — am einfachsten via `sub_nav()`-Macro (siehe unten).

---

## 3. Macros

```jinja
{% from "components/icons.html" import icon %}
{% from "components/ui.html" import badge, page_header, empty_state, avatar, sub_nav, stat_tile, action_form %}

{{ icon("cart", "h-5 w-5") }}
{{ badge("offen", "warning") }}
{{ page_header("Einkauf", "Was fehlt zuhause?", action_html) }}
{{ empty_state("cart", "Liste leer", "Füg oben was hinzu.") }}

{{ avatar(user) }}                                  {# size/text_class als literale Klassen, z.B. avatar(user, "h-16 w-16", "text-xl") #}
{{ sub_nav([(url_for('a.x'), 'x', 'Label'), …], aktiver_key) }}
{{ stat_tile(stats.points, "Punkte", "gold") }}     {# tone: brand|emerald|gold|rose #}
{# action_form kapselt ein HTMX-Aktions-Formular (approve/reject/toggle-role): #}
{{ action_form(url_for('admin.approve', user_id=u.id), "Freischalten", "primary", "check", "#user-row-" ~ u.id) }}
```

**Icon-Namen:** `home check check-circle cart calendar users shield plus trash undo x logout user
clock alert sparkle chevron-right`. Icons färben sich per `currentColor` (Textfarbe). Neues Icon
gebraucht? → SVG-Pfad in `components/icons.html` ergänzen (Lucide-Stil: stroke, runde Enden).

`page_header(title, subtitle="", action="")` — `action` ist fertiges HTML (z. B. ein Button), optional.
Tipp: Button per `{% set btn %}…{% endset %}` bauen und übergeben.

`avatar(user, size, text_class)` ersetzt die früher kopierte Avatar-Markup (Bild **oder** Initiale).
`sub_nav(items, active)` rendert die Pill-Sub-Navigation; `items` = Liste `(href, key, label)`.
`action_form(action_url, label, variant, icon_name, hx_target, hidden={}, confirm=None, block=True)`
baut ein `<form>` mit `hx-post`/`hx-target`/`hx-swap="outerHTML"` + Button — `hidden` = Dict mit
versteckten Feldern (`name: value`). Varianten/Badge-Klassen werden intern über eine **Literal-Map**
aufgelöst (kein dynamisches Zusammenbauen).

---

## 4. Navigation & Active-State

- **Desktop (`md+`):** Leiste oben. **Handy:** Tab-Leiste unten (Daumen-Zone). Ziel: **max. 5 Tabs**.
- Top-Level-Tabs: `Übersicht · Aufgaben · Einkauf · Abwesenheit · Verwaltung` (letzteres nur für
  Hauswart/Admin). „Verwaltung" bündelt Hauswart + Admin + Extra-Prüfung. „Extra" hat **keinen**
  eigenen Tab — Einstieg über die CTA im Dashboard-Score-Footer.
- `nav_items`-Tupel = `(endpoint, blueprint, label, icon, match_bps)`. `match_bps` ist die Liste der
  Blueprints, bei denen der Tab aktiv leuchtet (z.B. Verwaltung = `['hauswart','admin','extras']`).
  Active-State über `request.blueprint in match_bps`.
- **Sub-Navigation innerhalb eines Bereichs** (Verwaltung → Aufgaben prüfen/Nutzer/Extra-Leistungen,
  Aufgaben → Woche/Liste/Neu): `sub_nav()`-Macro oben im Content. Cross-Blueprint-Bereiche keyen auf
  `request.blueprint`, Intra-Blueprint (Aufgaben) auf `?view=` bzw. das Endpoint. Verwaltungs-Sub-Nav
  liegt zentral in `components/_verwaltung_subnav.html`.
- **Neue Top-Level-Seite?** → Tupel in `nav_items` ergänzen, dann automatisch in Top-Bar **und** Bottom-Tabs.
- `main` hat unten `pb-28`, damit nichts hinter den Handy-Tabs verschwindet — bei neuen Layouts beibehalten.

---

## 5. Font & Flash

- Fonts via Google-Fonts-`<link>` in `base.html` (Fallback `system-ui`): **Varela Round** als
  `font-display` (Überschriften — `h1/h2/h3` + `.page-title` + `.stat-value` bekommen das automatisch),
  **Nunito** als `font-sans` (Fließtext). Optionaler Zukunfts-Schritt: Fonts selbst hosten (woff2 unter
  `static/fonts/`) für Offline-Betrieb.
- **Flash-Messages** werden zentral in `base.html` (`components/_flash.html`) gerendert.
  → In Feature-Templates **keine** eigenen `get_flashed_messages`-Blöcke mehr. Kategorien:
  `success` (grün), `error` (rose), sonst Info (violett).

---

## 6. Do / Don't — für alles Neue

**DO**
- Neue Buttons immer `.btn` + Variante. Neue Boxen immer `.card`. Neue Formularfelder immer `.field/.label/.input`.
- Runde Ecken (`rounded-xl/2xl`) + `shadow-soft` = der Look. Großzügig Weißraum lassen.
- Status/Labels über `badge()`; Icons über `icon()`.
- Eine Aktion = ein klarer Button. Lieber eine Sache gut sichtbar als fünf gleichwertige.
- Partials (Dateien mit `_…` oder unter `_components/`), die per HTMX getauscht werden, müssen
  **standalone** rendern → benötigte `{% from "components/…" import … %}` ganz oben im Partial.

**DON'T**
- **Klassennamen niemals dynamisch zusammenstückeln** (`"badge-" ~ variant`) — Tailwind scannt
  nur literalen Text und würde die Klasse weglassen. Stattdessen volle Klassennamen oder das
  `badge()`-Macro (das hat alle Varianten literal drin).
- Keine HTMX-Attribute, `hx-target`/`hx-swap`, Element-`id`s oder Formular-`name`s beim Stylen
  ändern — daran hängt die Funktion. Nur Klassen/Markup anfassen.
- Kein neues Grau/Slate als Flächenfarbe einführen — `surface` + `white` reichen.
- Keine Roh-`<button class="px-3 py-1 bg-…">` mehr — das ist genau das alte Chaos.

---

## 7. Build

```powershell
# Nach JEDER Template-/CSS-Änderung neu bauen (Tailwind scannt die Templates):
tools\tailwindcss.exe -i app/static/css/input.css -o app/static/css/output.css

# Beim Entwickeln: Watch-Modus (baut bei Template-Änderungen automatisch)
tools\tailwindcss.exe -i app/static/css/input.css -o app/static/css/output.css --watch
```

`output.css` ist generiert/gitignored — nie von Hand editieren. Quelle ist `input.css` + die Templates.
