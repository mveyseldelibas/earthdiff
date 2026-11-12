"""A small, dependency-free .xlsx writer (Office Open XML) for the change report, and the workbook earthdiff writes.

Sheets: summary (what was compared, totals, warnings), changes (one row per polygon, with a plain-language
description and a map link) and scenes. Numbers are real numbers in Excel, so they can be sorted, filtered and summed."""
from __future__ import annotations

import io
import re
import zipfile
from typing import Any, Iterable
from xml.sax.saxutils import escape

from .i18n import CAUSE_SHORT, STRENGTH, class_label, describe

_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_SHEET_BAD = re.compile(r"[\[\]:*?/\\]")  # characters Excel does not allow in sheet names
H, NUM2, NUM3, LINK, WRAP = 1, 2, 3, 4, 5  # cell styles


class Link:
    def __init__(self, url: str, text: str):
        self.url, self.text = url, text


class Styled:
    def __init__(self, value: Any, style: int):
        self.value, self.style = value, style


def _col(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(ref: str, v: Any, style: int = 0) -> str:
    if isinstance(v, Styled):
        return _cell(ref, v.value, v.style)
    st = f' s="{style}"' if style else ""
    if v is None or v == "":
        return f'<c r="{ref}"{st}/>' if style else ""
    if isinstance(v, bool):
        v = str(v)
    if isinstance(v, (int, float)):
        if v != v or v in (float("inf"), float("-inf")):  # NaN / inf: leave the cell empty
            return ""
        return f'<c r="{ref}"{st}><v>{v}</v></c>'
    if isinstance(v, Link):
        f = 'HYPERLINK("%s","%s")' % (v.url.replace('"', ""), v.text.replace('"', ""))
        return f'<c r="{ref}" s="{LINK}" t="str"><f>{escape(f)}</f><v>{escape(v.text)}</v></c>'
    text = escape(_BAD.sub("", str(v)))
    return f'<c r="{ref}" t="inlineStr"{st}><is><t xml:space="preserve">{text}</t></is></c>'


def _sheet(rows: list[list[Any]], widths: list[float] | None, header: bool, styles: list[int] | None) -> str:
    out = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, v in enumerate(row):
            style = H if (header and r == 1) else ((styles[c] if styles and c < len(styles) else 0) if not isinstance(v, Styled) else 0)
            cells.append(_cell(f"{_col(c)}{r}", v, style))
        out.append(f'<row r="{r}">{"".join(cells)}</row>')
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths or []))
    ncol = max((len(r) for r in rows), default=1)
    view = '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>' if header else ""
    filt = f'<autoFilter ref="A1:{_col(ncol - 1)}{len(rows)}"/>' if header and len(rows) > 1 else ""
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'{view}{"<cols>" + cols + "</cols>" if cols else ""}<sheetData>{"".join(out)}</sheetData>{filt}</worksheet>')


STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
          '<numFmts count="2"><numFmt numFmtId="164" formatCode="0.00"/><numFmt numFmtId="165" formatCode="0.000"/></numFmts>'
          '<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font>'
          '<font><u/><color rgb="FF0563C1"/><sz val="11"/><name val="Calibri"/></font></fonts>'
          '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
          '<fill><patternFill patternType="solid"><fgColor rgb="FFDCE6F2"/><bgColor indexed="64"/></patternFill></fill></fills>'
          '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
          '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
          '<cellXfs count="6"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
          '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
          '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
          '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
          '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
          '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf></cellXfs>'
          '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


def workbook_bytes(sheets: Iterable[dict[str, Any]]) -> bytes:
    """``sheets``: dicts with name, rows (list of lists), optional widths, header (bool), styles (per column)."""
    sheets = list(sheets)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        over = "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                       for i in range(1, len(sheets) + 1))
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   f'<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>{over}</Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        clean = [escape(_SHEET_BAD.sub(" ", s["name"])[:31]) for s in sheets]  # outside the f-string: Python 3.11 allows no backslash there
        names = "".join(f'<sheet name="{n}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(clean, start=1))
        z.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   f'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{names}</sheets></workbook>')
        rels = "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                       for i in range(1, len(sheets) + 1))
        rels += f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        z.writestr("xl/_rels/workbook.xml.rels", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
        z.writestr("xl/styles.xml", STYLES)
        for i, s in enumerate(sheets, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet(s["rows"], s.get("widths"), s.get("header", True), s.get("styles")))
    return buf.getvalue()


TXT = {
    "en": {"summary": "Summary", "changes": "Changes", "scenes": "Scenes", "item": "Item", "value": "Value",
           "name": "Name", "before": "Before (baseline)", "after": "After", "area": "Area (km²)", "res": "Pixel size (m)",
           "comparable": "Comparable share of the area (%)", "scenes_n": "Scenes used (before + after)", "mmu": "Smallest change mapped (ha)",
           "sens": "Sensitivity", "total": "Total", "polys": "polygons", "cause": "Vegetation loss, likely cause (a hint)", "warn": "Warning",
           "note": "Note", "note_v": "Automated screening from free Sentinel-2 imagery: verify important findings with higher-resolution data or in the field.",
           "data": "Data", "made": "Made with", "cols": ["#", "What changed", "Description", "Area (ha)", "Strength", "Confidence (0-1)", "Likely cause", "Seasonal",
                                                        "NDVI before", "NDVI after", "MNDWI before", "MNDWI after", "BSI before", "BSI after", "dNBR", "When (largest drop)", "Latitude", "Longitude", "Map"],
           "scols": ["Period", "Scene", "Date", "Scene cloud (%)", "Cloud-free share of the area (%)"], "yes": "yes", "map": "open map"},
    "tr": {"summary": "Özet", "changes": "Değişimler", "scenes": "Sahneler", "item": "Konu", "value": "Değer",
           "name": "Ad", "before": "Önce (karşılaştırma)", "after": "Sonra", "area": "Alan (km²)", "res": "Piksel boyutu (m)",
           "comparable": "Alanın karşılaştırılabilir payı (%)", "scenes_n": "Kullanılan sahneler (önce + sonra)", "mmu": "Haritalanan en küçük değişim (ha)",
           "sens": "Duyarlılık", "total": "Toplam", "polys": "poligon", "cause": "Bitki kaybı, olası neden (ipucu)", "warn": "Uyarı",
           "note": "Not", "note_v": "Ücretsiz Sentinel-2 görüntüleriyle otomatik tarama: önemli bulguları daha yüksek çözünürlüklü veriyle ya da arazide doğrulayın.",
           "data": "Veri", "made": "Üreten", "cols": ["#", "Ne değişti", "Açıklama", "Alan (ha)", "Güç", "Güven (0-1)", "Olası neden", "Mevsimsel",
                                                    "NDVI önce", "NDVI sonra", "MNDWI önce", "MNDWI sonra", "BSI önce", "BSI sonra", "dNBR", "Ne zaman (en büyük düşüş)", "Enlem", "Boylam", "Harita"],
           "scols": ["Dönem", "Sahne", "Tarih", "Sahne bulut (%)", "Alanın bulutsuz payı (%)"], "yes": "evet", "map": "haritada aç"},
}


def report_workbook(lang: str, meta: dict, totals: dict, warnings: list[str], geojson: dict, scenes: list[dict],
                    extras: dict | None, attribution: str, terrain_attribution: str | None = None) -> bytes:
    lang = "tr" if lang == "tr" else "en"
    t = TXT[lang]
    summary: list[list[Any]] = [[t["item"], t["value"]],
                                [t["name"], meta.get("name")], [t["before"], meta.get("before_label")], [t["after"], meta.get("after_label")],
                                [t["area"], Styled(meta.get("aoi_km2"), NUM2)], [t["res"], meta.get("resolution_m")],
                                [t["comparable"], Styled(round(100 * float(meta.get("valid_fraction", 0)), 1), NUM2)],
                                [t["scenes_n"], f'{meta.get("scenes_before")} + {meta.get("scenes_after")}'],
                                [t["mmu"], meta.get("mmu_ha")], [t["sens"], meta.get("sensitivity")]]
    for key, tt in totals.items():
        summary.append([f'{t["total"]}: {class_label(key, lang)} (ha)', Styled(round(float(tt.get("area_ha", 0)), 2), NUM2)])
        summary.append([f'{t["total"]}: {class_label(key, lang)} ({t["polys"]})', int(tt.get("polygons", 0))])
    cause = (extras or {}).get("cause")
    if cause:
        for k, v in cause.get("totals", {}).items():
            summary.append([f'{t["cause"]}: {CAUSE_SHORT[lang].get(k, k)} (ha)', Styled(round(float(v.get("area_ha", 0)), 2), NUM2)])
    for w in warnings:
        summary.append([t["warn"], Styled(w, WRAP)])
    summary.append([t["note"], Styled(t["note_v"], WRAP)])
    summary.append([t["data"], Styled(attribution + ("; " + terrain_attribution if terrain_attribution else ""), WRAP)])
    summary.append([t["made"], f'earthdiff {meta.get("version")} ({meta.get("generated")})'])

    feats = sorted(geojson.get("features", []), key=lambda f: -float(f["properties"].get("area_ha", 0)))
    changes: list[list[Any]] = [t["cols"]]
    for f in feats:
        p = f["properties"]
        lat, lon = p.get("centroid_lat"), p.get("centroid_lon")
        url = f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=16/{lat}/{lon}"
        changes.append([p.get("id"), class_label(p.get("class", ""), lang), describe(p, lang), p.get("area_ha"),
                        STRENGTH[lang].get(p.get("confidence_level"), p.get("confidence_level")), p.get("confidence"),
                        CAUSE_SHORT[lang].get(p.get("likely_cause"), p.get("likely_cause") or ""), t["yes"] if p.get("likely_seasonal") is True else "",
                        p.get("ndvi_before"), p.get("ndvi_after"), p.get("mndwi_before"), p.get("mndwi_after"), p.get("bsi_before"), p.get("bsi_after"),
                        p.get("dnbr"), (f'{p["event_before"]} - {p["event_after"]}' if p.get("event_after") else ""), lat, lon, Link(url, t["map"])])
    sc_rows: list[list[Any]] = [t["scols"]]
    for s in scenes:
        sc_rows.append([s.get("period"), s.get("id"), str(s.get("date", ""))[:10], s.get("cloud_cover"), round(100 * float(s.get("valid_fraction", 0)), 1)])
    return workbook_bytes([
        {"name": t["summary"], "rows": summary, "widths": [44, 90], "header": True},
        {"name": t["changes"], "rows": changes, "widths": [6, 22, 70, 10, 10, 10, 14, 10, 10, 10, 11, 11, 10, 10, 8, 24, 11, 11, 13],
         "styles": [0, WRAP, WRAP, NUM2, 0, NUM2, 0, 0, NUM3, NUM3, NUM3, NUM3, NUM3, NUM3, NUM3, 0, 0, 0, 0]},
        {"name": t["scenes"], "rows": sc_rows, "widths": [34, 44, 12, 14, 26]},
    ])
