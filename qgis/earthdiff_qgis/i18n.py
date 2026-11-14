"""Turkish / English texts of the plugin. The language follows the QGIS user interface (Settings > Options > General)."""
from qgis.core import QgsSettings

TEXT = {
    "en": {
        "title": "earthdiff: what changed here?",
        "menu": "earthdiff: Sentinel-2 change detection",
        "intro": "Compares free Sentinel-2 imagery of two periods and maps what changed, in hectares. "
                 "No account or API key; the analysis runs on this computer.",
        "area": "1. Area (at most {max} km²)",
        "a_extent": "Current map view",
        "a_draw": "Rectangle drawn on the map",
        "a_sel": "Selected polygons of a layer",
        "draw": "Draw",
        "draw_hint": "Drag a rectangle on the map.",
        "size": "Area: {km2} km²",
        "size_none": "Area: not set",
        "too_big": "Area: {km2} km², more than {max} km². Zoom in or draw a smaller area.",
        "no_sel": "Select one or more polygons in the chosen layer.",
        "dates": "2. Dates",
        "after": "After (recent month)",
        "before": "Before: year to compare with",
        "same_month": "Same month as \"after\", so both are the same season.",
        "years_ago": "{y} ({n} year earlier)",
        "years_ago_n": "{y} ({n} years earlier)",
        "opts": "3. Settings",
        "sens": "Sensitivity",
        "s_strict": "strict (fewer, surer changes)",
        "s_default": "default",
        "s_sensitive": "sensitive (more, weaker changes)",
        "mmu": "Smallest change",
        "cause": "Likely cause hint (vegetation loss)",
        "cause_tip": "Labels vegetation loss as fire-like, clearing-like or unclear (dNBR from band B12, brightness and shape). A hint, not a verdict.",
        "seasonal": "Seasonal check (slower)",
        "seasonal_tip": "Compares vegetation loss with the same months of earlier years and flags loss that repeats every year (harvest-like). Adds a few small downloads.",
        "terrain": "3D terrain in the report",
        "terrain_tip": "Reads the Copernicus DEM (GLO-30) so the HTML report has an offline 3D view and a relief basemap.",
        "name": "Name (optional)",
        "name_ph": "e.g. Manavgat fire",
        "adv": "Output and earthdiff command",
        "out": "Output folder",
        "browse": "Browse…",
        "cmd": "earthdiff command",
        "cmd_auto": "automatic",
        "cmd_found": "Found: {c}",
        "cmd_none": "earthdiff was not found. Install it in a normal Python (not QGIS's): <code>pip install earthdiff</code>, "
                    "or install <a href=\"https://docs.astral.sh/uv/\">uv</a> (then it runs with <code>uvx</code>). "
                    "Or pick the earthdiff program below.",
        "cmd_pick": "Pick…",
        "cmd_test": "Test",
        "plan": "Plan (no download)",
        "run": "Run analysis",
        "stop": "Stop",
        "open": "Open report",
        "folder": "Open folder",
        "new": "New analysis",
        "new_tip": "Clears the drawn area and the result in this panel; asks whether to remove the result layers from the map.",
        "clear_title": "earthdiff results",
        "clear_q": "Remove the result layers of this panel ({n} group(s)) from the map and go back to the earlier map view?\n"
                   "The files stay in the output folder.",
        "remove": "Remove",
        "keep": "Keep on the map",
        "running": "Running… (usually 1-4 minutes; the first start with uvx downloads about 200 MB once)",
        "stopped": "Stopped.",
        "nostart": "earthdiff could not be started. Check the earthdiff command below (or leave it empty for automatic).",
        "future": "The \"after\" month has not begun yet. Pick this month or an earlier one.",
        "failed": "earthdiff stopped with an error (code {code}). The last lines of the log say why.",
        "done": "Done. Layers added to the project.",
        "res_title": "Result: {name}",
        "comparable": "Comparable (cloud-free in both periods): {p}% of the area",
        "polys": "{n} polygons",
        "cause_line": "Vegetation loss, likely cause (a hint, not a verdict): fire-like {f} ha · clearing-like {c} ha · unclear {u} ha",
        "seasonal_line": "Likely seasonal (repeats every year): {a} ha",
        "warnings": "Warnings",
        "no_warnings": "No warnings.",
        "screening": "A screening tool: it shows what changed, not why. Not an official damage assessment; verify before acting.",
        "g_group": "earthdiff · {name}",
        "l_changes": "Changes (polygons)",
        "l_classes": "Change classes (raster)",
        "l_after": "After · {lbl}",
        "l_before": "Before · {lbl}",
        "c_vegetation_loss": "Vegetation loss",
        "c_vegetation_gain": "Vegetation gain",
        "c_new_water": "New water",
        "c_water_loss": "Water loss",
        "c_new_bare_or_built": "New bare soil / built-up",
        "busy": "An analysis is already running.",
        "need_area": "Set the area first.",
        "drawn": "Drawn area",
        "extent": "Map view",
        "months": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
    },
    "tr": {
        "title": "earthdiff: burada ne değişti?",
        "menu": "earthdiff: Sentinel-2 değişim tespiti",
        "intro": "İki dönemin ücretsiz Sentinel-2 görüntülerini karşılaştırır ve neyin değiştiğini hektar olarak haritalar. "
                 "Hesap ya da API anahtarı gerekmez; analiz bu bilgisayarda çalışır.",
        "area": "1. Alan (en çok {max} km²)",
        "a_extent": "Haritada görünen bölge",
        "a_draw": "Haritada çizilen dikdörtgen",
        "a_sel": "Bir katmanın seçili poligonları",
        "draw": "Çiz",
        "draw_hint": "Haritada bir dikdörtgen sürükleyin.",
        "size": "Alan: {km2} km²",
        "size_none": "Alan: seçilmedi",
        "too_big": "Alan: {km2} km², {max} km²'den büyük. Yakınlaşın ya da daha küçük bir alan çizin.",
        "no_sel": "Seçilen katmanda bir ya da daha çok poligon seçin.",
        "dates": "2. Tarihler",
        "after": "Sonra (yakın ay)",
        "before": "Önce: karşılaştırılacak yıl",
        "same_month": "\"Sonra\" ile aynı ay; böylece ikisi de aynı mevsimde olur.",
        "years_ago": "{y} ({n} yıl önce)",
        "years_ago_n": "{y} ({n} yıl önce)",
        "opts": "3. Ayarlar",
        "sens": "Duyarlılık",
        "s_strict": "katı (daha az, daha kesin değişim)",
        "s_default": "varsayılan",
        "s_sensitive": "duyarlı (daha çok, daha zayıf değişim)",
        "mmu": "En küçük değişim",
        "cause": "Olası neden ipucu (bitki kaybı)",
        "cause_tip": "Bitki kaybını yangına benzer, temizlemeye benzer ya da belirsiz diye etiketler (B12 bandından dNBR, parlaklık ve şekil). Hüküm değil, ipucu.",
        "seasonal": "Mevsim kontrolü (daha yavaş)",
        "seasonal_tip": "Bitki kaybını önceki yılların aynı aylarıyla karşılaştırır, her yıl tekrarlayan (hasat benzeri) kaybı işaretler. Birkaç küçük indirme ekler.",
        "terrain": "Raporda 3B arazi",
        "terrain_tip": "Copernicus DEM (GLO-30) okunur; HTML raporda internetsiz 3B görünüm ve kabartma altlığı olur.",
        "name": "Ad (isteğe bağlı)",
        "name_ph": "ör. Manavgat yangını",
        "adv": "Çıktı ve earthdiff komutu",
        "out": "Çıktı klasörü",
        "browse": "Gözat…",
        "cmd": "earthdiff komutu",
        "cmd_auto": "otomatik",
        "cmd_found": "Bulundu: {c}",
        "cmd_none": "earthdiff bulunamadı. QGIS'in değil, normal bir Python'a kurun: <code>pip install earthdiff</code>, "
                    "ya da <a href=\"https://docs.astral.sh/uv/\">uv</a> kurun (o zaman <code>uvx</code> ile çalışır). "
                    "Ya da aşağıdan earthdiff programını seçin.",
        "cmd_pick": "Seç…",
        "cmd_test": "Dene",
        "plan": "Plan (indirmeden)",
        "run": "Analizi çalıştır",
        "stop": "Durdur",
        "open": "Raporu aç",
        "folder": "Klasörü aç",
        "new": "Yeni analiz",
        "new_tip": "Çizilen alanı ve bu paneldeki sonucu temizler; sonuç katmanlarının haritadan kaldırılıp kaldırılmayacağını sorar.",
        "clear_title": "earthdiff sonuçları",
        "clear_q": "Bu panelin sonuç katmanları ({n} grup) haritadan kaldırılsın ve harita önceki görünümüne dönsün mü?\n"
                   "Dosyalar çıktı klasöründe kalır.",
        "remove": "Kaldır",
        "keep": "Haritada bırak",
        "running": "Çalışıyor… (genellikle 1-4 dakika; uvx ile ilk açılışta bir kez yaklaşık 200 MB indirilir)",
        "stopped": "Durduruldu.",
        "nostart": "earthdiff başlatılamadı. Aşağıdaki earthdiff komutunu kontrol edin (otomatik için boş bırakın).",
        "future": "\"Sonra\" ayı henüz başlamadı. Bu ayı ya da daha önceki bir ayı seçin.",
        "failed": "earthdiff hatayla durdu (kod {code}). Nedeni günlüğün son satırlarında.",
        "done": "Bitti. Katmanlar projeye eklendi.",
        "res_title": "Sonuç: {name}",
        "comparable": "Karşılaştırılabilir alan (iki dönemde de bulutsuz): %{p}",
        "polys": "{n} poligon",
        "cause_line": "Bitki kaybı, olası neden (ipucu, hüküm değil): yangına benzer {f} ha · temizlemeye benzer {c} ha · belirsiz {u} ha",
        "seasonal_line": "Olası mevsimsel (her yıl tekrarlıyor): {a} ha",
        "warnings": "Uyarılar",
        "no_warnings": "Uyarı yok.",
        "screening": "Ön tarama aracıdır: neyin değiştiğini gösterir, nedenini değil. Resmî hasar tespiti değildir; karar vermeden önce doğrulayın.",
        "g_group": "earthdiff · {name}",
        "l_changes": "Değişimler (poligon)",
        "l_classes": "Değişim sınıfları (raster)",
        "l_after": "Sonra · {lbl}",
        "l_before": "Önce · {lbl}",
        "c_vegetation_loss": "Bitki kaybı",
        "c_vegetation_gain": "Bitki artışı",
        "c_new_water": "Yeni su",
        "c_water_loss": "Su kaybı",
        "c_new_bare_or_built": "Yeni çıplak toprak / yapılaşma",
        "busy": "Zaten bir analiz çalışıyor.",
        "need_area": "Önce alanı belirleyin.",
        "drawn": "Çizilen alan",
        "extent": "Harita görünümü",
        "months": ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"],
    },
}


def lang() -> str:
    forced = str(QgsSettings().value("earthdiff/lang", "") or "")  # for tests and for users who want the other language
    if forced in TEXT:
        return forced
    try:
        from qgis.core import QgsApplication
        loc = QgsApplication.locale() or ""
    except Exception:
        loc = ""
    if not loc:
        from qgis.PyQt.QtCore import QLocale
        loc = QLocale.system().name()
    return "tr" if loc.lower().startswith("tr") else "en"


def num(x, d: int = 1) -> str:
    """1234.5 -> '1,234.5' (English) or '1.234,5' (Turkish)."""
    s = f"{float(x):,.{d}f}"
    return s.translate(str.maketrans(",.", ".,")) if lang() == "tr" else s


def t(key: str, **kw):
    v = TEXT[lang()].get(key, TEXT["en"].get(key, key))
    return v.format(**kw) if kw and isinstance(v, str) else v
