"""Self-contained HTML report (Leaflet inlined, imagery embedded as data URLs)."""
from __future__ import annotations

import base64
import html
import json
from importlib import resources
from typing import Any

from .change import CLASSES
from .i18n import CLASS_LABELS_TR, basis_tr, method_html_tr, warning_tr
from .xlsx import report_workbook

_TEMPLATE_PKG = "earthdiff"


def _read(rel: str) -> str:
    return resources.files(_TEMPLATE_PKG).joinpath(rel).read_text(encoding="utf-8")


def data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def method_html(thresholds: dict[str, float], mmu_ha: float, resolution: float) -> str:
    t = thresholds
    rows = "".join(
        f"<tr><td><span class='chip' style='background:{c.color}'></span>{html.escape(c.label)}</td><td><code>{html.escape(c.rule)}</code></td></tr>"
        for c in CLASSES.values()
    )
    return f"""
<p>earthdiff compares two <b>median composites</b> of Sentinel-2 L2A surface reflectance. Cloud, cloud shadow, cirrus, snow and
saturated pixels are removed with the scene classification layer (SCL, cloud pixels buffered by 30&nbsp;m) before the per-pixel median is taken,
so each pixel is the median of its cloud-free observations in that period. Everything below is a plain, inspectable rule &ndash; no machine-learning model.</p>
<table><thead><tr><th>Change class</th><th>Rule (d = after &minus; before)</th></tr></thead><tbody>{rows}</tbody></table>
<pre>NDVI  = (NIR - Red) / (NIR + Red)                      B08, B04
MNDWI = (Green - SWIR1) / (Green + SWIR1)              B03, B11
BSI   = ((SWIR1 + Red) - (NIR + Blue)) / ((SWIR1 + Red) + (NIR + Blue))   B11, B04, B08, B02</pre>
<p>Pixel rules are then cleaned: features narrower than 3 pixels are removed by a morphological opening (3x3) (for pixels &le; 20&nbsp;m), blobs smaller than the
<b>minimum mapping unit ({mmu_ha:g}&nbsp;ha)</b> are dropped, and the rest are polygonized (8-connectivity). Areas are pixel counts &times; pixel area
({resolution:g}&nbsp;m pixels) on a UTM grid.</p>
<p><b>Confidence</b> is a heuristic score from 0 to 1 &mdash; <i>not</i> a probability: 0.5&times;magnitude (how far the mean index change exceeds the threshold, saturating at 2&times;)
+ 0.3&times;valid observations (min of before/after, saturating at 3) + 0.2&times;size (log10 of area / MMU, saturating at 10&times;MMU). &ge;0.70 is &ldquo;high&rdquo;, &ge;0.45 &ldquo;medium&rdquo;.</p>
<h3>Likely cause, seasonal check, bright surfaces</h3>
<p><b>Likely cause</b> (vegetation-loss polygons only) is a hint, not a verdict: <code>fire-like</code> needs a dNBR of at least 0.27 (moderate-low burn severity of Key &amp; Benson 2006, the thresholds also used by Copernicus EFFIS)
plus a dark post-event surface and more evidence for fire (also an irregular outline) than for clearing; <code>clearing-like</code> means little burn signal or a straight-edged, bright outline (felling, harvest, ploughing, construction); everything else is <code>unclear</code>.
The brightness and shape thresholds are this project's own heuristics. When the two windows are years apart, each vegetation-loss polygon is also followed through one pass per in-between year; if one year-to-year step drops NBR much more than the whole period does (an event that has partly recovered since, such as an old burn), the hint is judged on that step and the report says when it happened. <b>Likely seasonal</b> (optional check) flags polygons whose NDVI fell by a similar amount in the same windows of earlier years; polygons are never removed.
<b>Bright surfaces</b> (salt, sand, snow: mean visible reflectance &ge; 0.25 and NDVI &lt; 0.15) saturate the imagery; when they cover a large share of the area the report warns that change there is not reliable and lowers the confidence of polygons on them.</p>
<h3>Limitations &mdash; please read</h3>
<ul>
<li><b>Seasonality and agriculture.</b> Crops, grassland and deciduous forest change colour with the seasons and with management. Compare the same season in different years; harvest, ploughing, irrigation and grazing look like vegetation loss/gain.</li>
<li><b>Not a burn-severity or forest-law product.</b> &ldquo;Vegetation loss&rdquo; says vegetation greenness fell; it does not say why (fire, felling, drought, harvest, construction). &ldquo;New bare/built-up&rdquo; can also be soil moisture, dried crops or exposed lake bed.</li>
<li><b>Water.</b> Shallow, turbid or algae-covered water and wet soil can fall on either side of the MNDWI rule; cloud shadow and terrain shadow can mimic water.</li>
<li><b>Resolution.</b> Sentinel-2 pixels are {resolution:g}&nbsp;m here (SWIR is natively 20&nbsp;m). Features narrower than a few pixels (roads, small buildings, narrow streams) are not reliably detected. The MMU hides everything smaller than {mmu_ha:g}&nbsp;ha.</li>
<li><b>Atmosphere and geometry.</b> Residual haze, sun/view-angle differences between dates and scene co-registration (~10&nbsp;m) create false alarms along edges.</li>
<li><b>No ground truth.</b> Results are screening hints. Verify anything consequential with higher-resolution imagery, field data or official sources.</li>
</ul>"""


def render_report(
    *,
    title: str,
    meta: dict[str, Any],
    totals: dict[str, Any],
    warnings: list[str],
    geojson: dict[str, Any],
    aoi_geojson: dict[str, Any],
    bounds: list[list[float]],
    images: dict[str, str],
    scenes: list[dict[str, Any]],
    thresholds: dict[str, float],
    attribution: str,
    extras: dict[str, Any] | None = None,
    timeline: dict[str, Any] | None = None,
    terrain: dict[str, Any] | None = None,
) -> str:
    data = {
        "meta": meta,
        "totals": totals,
        "warnings": warnings,
        "geojson": geojson,
        "aoi": aoi_geojson,
        "bounds": bounds,
        "img": images,
        "scenes": scenes,
        "thresholds": thresholds,
        "attribution": attribution,
        "extras": extras or {},
        "timeline": timeline or {"frames": [], "bounds": None},
        "terrain": terrain,
        "classes": {c.key: {"label": c.label, "color": c.color} for c in CLASSES.values()},
        "method_html": method_html(thresholds, meta["mmu_ha"], meta["resolution_m"]),
        # Turkish texts; the page switches language in the browser (English stays the default for non-Turkish browsers)
        "i18n": {"tr": {
            "warnings": [warning_tr(w) for w in warnings],
            "method_html": method_html_tr(CLASSES.values(), meta["mmu_ha"], meta["resolution_m"]),
            "classes": CLASS_LABELS_TR,
        }},
    }
    # the report's Excel button downloads one of these, in the language shown
    data["xlsx"] = {lg: base64.b64encode(report_workbook(lg, meta, totals, warnings if lg == "en" else data["i18n"]["tr"]["warnings"], geojson, scenes, extras,
                                                       attribution, (terrain or {}).get("attribution"))).decode("ascii") for lg in ("en", "tr")}
    for f in geojson.get("features", []):
        p = f.get("properties", {})
        if p.get("likely_cause_basis"):
            p["likely_cause_basis_tr"] = basis_tr(p["likely_cause_basis"])
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace("\u2028", "\\u2028")
    page = _read("templates/report.html")
    # placeholders are replaced with plain str.replace so the big blobs are never re-scanned by a regex
    page = page.replace("{{TITLE}}", html.escape(title))
    page = page.replace("{{LEAFLET_CSS}}", _read("vendor/leaflet.css"))
    page = page.replace("{{LEAFLET_JS}}", _read("vendor/leaflet.js").replace("</script", "<\\/script"))
    return page.replace("{{DATA_JSON}}", payload)
