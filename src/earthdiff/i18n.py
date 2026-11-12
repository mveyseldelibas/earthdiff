"""Turkish texts for the HTML report (the report switches between English and Turkish in the browser).

Warnings and cause hints are produced in English by the pipeline; this module maps each known sentence to Turkish.
An unknown sentence falls back to English, and ``tests/test_i18n.py`` checks that every warning the pipeline can
produce has a translation."""
from __future__ import annotations

import html
import re
from typing import Callable

CLASS_LABELS_TR = {
    "vegetation_loss": "Bitki kaybı",
    "vegetation_gain": "Bitki artışı",
    "new_water": "Yeni su",
    "water_loss": "Su kaybı",
    "new_bare_or_built": "Yeni çıplak toprak / yapılaşma",
}

PERIOD_TR = {"before": "önce", "after": "sonra"}


def tr_num(s: str) -> str:
    """1,234.5 -> 1.234,5 (Turkish digit grouping and decimal comma)."""
    return s.replace(",", "\0").replace(".", ",").replace("\0", ".")


Sub = Callable[[re.Match], str]

_WARNINGS: list[tuple[re.Pattern, Sub]] = [
    (re.compile(r"The two windows are (\d+) days apart in the seasonal cycle\. Vegetation and water change naturally with the season: "
                r"compare the same time of year in different years to avoid false alarms\."),
     lambda m: f"İki dönem mevsim döngüsünde {m[1]} gün arayla. Bitki örtüsü ve su mevsimle doğal olarak değişir: "
               "yanlış alarmdan kaçınmak için farklı yılların aynı dönemini karşılaştırın."),
    (re.compile(r"A window shorter than two weeks usually contains very few cloud-free scenes; consider a longer window\."),
     lambda m: "İki haftadan kısa bir dönemde genellikle çok az bulutsuz sahne olur; daha uzun bir dönem seçin."),
    (re.compile(r"Only one usable scene in the (before|after) period: the composite is a single acquisition, so residual cloud/haze or one-off conditions are not averaged out\."),
     lambda m: f"{PERIOD_TR[m[1]].capitalize()} döneminde yalnız bir kullanılabilir sahne var: bileşim tek bir çekimden oluşuyor, "
               "bu yüzden kalan bulut, pus ya da tek seferlik koşullar ortalamayla giderilemiyor."),
    (re.compile(r"No usable scene in the (before|after) period\."),
     lambda m: f"{PERIOD_TR[m[1]].capitalize()} döneminde kullanılabilir sahne yok."),
    (re.compile(r"Only (\d+)% of the AOI is cloud-free in both periods; the remainder is excluded and not reported as 'no change'\."),
     lambda m: f"Alanın yalnız %{m[1]} kadarı iki dönemde de bulutsuz; geri kalanı hesaba katılmadı ve \"değişim yok\" diye raporlanmadı."),
    (re.compile(r"Changes cover (\d+)% of the AOI, and (\d+)% of the changed area is fire-like vegetation loss \(dNBR, an independent burn signal\): "
                r"this is consistent with one large burn rather than a seasonal or atmospheric mismatch\. Inspect the before/after images to confirm\."),
     lambda m: f"Alanın %{m[1]} kadarı değişmiş ve değişen alanın %{m[2]} kadarı yangın izine benzeyen bitki kaybı (dNBR, bağımsız bir yanık sinyali): "
               "bu, mevsim ya da atmosfer farkından çok tek büyük bir yangınla uyumlu. Doğrulamak için önce/sonra görüntülerine bakın."),
    (re.compile(r"Changes cover (\d+)% of the AOI: such a large share often means a seasonal, atmospheric or image-registration mismatch "
                r"rather than real land-cover change\. Inspect the before/after images\."),
     lambda m: f"Alanın %{m[1]} kadarı değişmiş görünüyor: bu kadar büyük bir oran çoğu zaman gerçek arazi değişiminden çok "
               "mevsim, atmosfer ya da görüntü çakıştırma farkı demektir. Önce/sonra görüntülerine bakın."),
    (re.compile(r"(\d+)% of the comparable area is very bright, non-vegetated surface \(salt, sand, snow or similar; (\d+)% in both periods\)\. "
                r"Change detection is NOT reliable on such surfaces: do not read the absence of change there as 'no change'\. "
                r"Confidence of polygons on bright pixels is lowered\."),
     lambda m: f"Karşılaştırılabilir alanın %{m[1]} kadarı çok parlak, bitkisiz yüzey (tuz, kum, kar ya da benzeri; iki dönemde de %{m[2]}). "
               "Bu yüzeylerde değişim tespiti güvenilir DEĞİL: orada değişim görünmemesini \"değişim yok\" diye okumayın. "
               "Parlak piksellerdeki poligonların güven puanı düşürüldü."),
    (re.compile(r"Exploratory signal on the bright surface: the share of water-like pixels \(NDWI > 0\) changed from (\d+)% to (\d+)%, "
                r"which suggests a bright surface \(a salt flat or shallow lake\) became (drier|wetter)\. The default MNDWI rule cannot see this "
                r"because dry salt also scores as water in MNDWI\. Not a mapped change: verify with other data\."),
     lambda m: f"Parlak yüzeyde deneysel sinyal: suya benzeyen piksellerin oranı (NDWI > 0) %{m[1]} → %{m[2]} oldu; bu, parlak bir yüzeyin "
               f"(tuz düzlüğü ya da sığ göl) {'kuruduğunu' if m[3] == 'drier' else 'ıslandığını'} düşündürüyor. Varsayılan MNDWI kuralı bunu göremez, "
               "çünkü kuru tuz da MNDWI'da su gibi görünür. Haritalanmış bir değişim değildir: başka verilerle doğrulayın."),
    (re.compile(r"likely_cause is unavailable: Sentinel-2 band B12 \(SWIR2\) could not be read for these scenes, so vegetation loss is not labelled fire-like / clearing-like\."),
     lambda m: "Neden ipucu yok: bu sahnelerde Sentinel-2 B12 (SWIR2) bandı okunamadı, bu yüzden bitki kaybı yangın ya da açma olarak etiketlenmedi."),
    (re.compile(r"The seasonal check found no usable scenes in earlier years \(windows before 2017 have no data\), so no polygon was checked\."),
     lambda m: "Mevsim kontrolü önceki yıllarda kullanılabilir sahne bulamadı (2017 öncesinde veri yok), bu yüzden hiçbir poligon kontrol edilmedi."),
    (re.compile(r"([\d,.]+) ha \((\d+)% of the vegetation loss, (\d+) polygons\) repeats at the same time of year in earlier years: "
                r"likely_seasonal \(harvest or a seasonal cycle rather than a one-off loss\)\. The polygons are kept and flagged\."),
     lambda m: f"{tr_num(m[1])} ha (bitki kaybının %{m[2]} kadarı, {m[3]} poligon) önceki yıllarda da yılın aynı zamanında tekrarlıyor: "
               "muhtemelen mevsimsel (tek seferlik kayıp değil, hasat ya da mevsim döngüsü). Poligonlar silinmedi, işaretlendi."),
]

_BASIS: list[tuple[re.Pattern, Sub]] = [
    (re.compile(r"largest one-step drop between (\S+) and (\S+)"), lambda m: f"en büyük tek adımlık düşüş {m[1]} ile {m[2]} arasında"),
    (re.compile(r"no SWIR2 \(B12\) measurement, so no burn signal could be computed"), lambda m: "SWIR2 (B12) ölçümü yok, yanık sinyali hesaplanamadı"),
    (re.compile(r"dNBR (-?[\d.]+) \(at least moderate-high burn severity by Key & Benson thresholds\)"),
     lambda m: f"dNBR {tr_num(m[1])} (Key & Benson eşiklerine göre en az orta-yüksek yanık şiddeti)"),
    (re.compile(r"dNBR (-?[\d.]+) \(at least moderate-low burn severity\)"), lambda m: f"dNBR {tr_num(m[1])} (en az orta-düşük yanık şiddeti)"),
    (re.compile(r"dNBR (-?[\d.]+) \(below 0\.10: no burn signal\)"), lambda m: f"dNBR {tr_num(m[1])} (0,10'un altında: yanık sinyali yok)"),
    (re.compile(r"dNBR (-?[\d.]+) \(low, 0\.10 to 0\.27: weak burn signal\)"), lambda m: f"dNBR {tr_num(m[1])} (düşük, 0,10-0,27: zayıf yanık sinyali)"),
    (re.compile(r"dark after \(visible reflectance (-?[\d.]+)\), like char or ash"),
     lambda m: f"sonrasında koyu yüzey (görünür yansıma {tr_num(m[1])}), kömür ya da kül gibi"),
    (re.compile(r"bright after \(visible reflectance (-?[\d.]+)\), like exposed soil or stubble"),
     lambda m: f"sonrasında parlak yüzey (görünür yansıma {tr_num(m[1])}), açılmış toprak ya da anız gibi"),
    (re.compile(r"straight-edged outline \(rectangularity (-?[\d.]+)\)"), lambda m: f"düz kenarlı sınır (dikdörtgenlik {tr_num(m[1])})"),
    (re.compile(r"irregular outline \(rectangularity (-?[\d.]+)\)"), lambda m: f"düzensiz sınır (dikdörtgenlik {tr_num(m[1])})"),
]


def _translate(text: str, table: list[tuple[re.Pattern, Sub]]) -> str | None:
    for pat, sub in table:
        m = pat.fullmatch(text)
        if m:
            return sub(m)
    return None


def warning_tr(text: str) -> str:
    """Turkish version of a pipeline warning; the English text if it is not known."""
    return _translate(text, _WARNINGS) or text


def has_warning_tr(text: str) -> bool:
    return _translate(text, _WARNINGS) is not None


def basis_tr(text: str) -> str:
    """Turkish version of a ``likely_cause_basis`` ('; '-joined sentences); unknown parts stay in English."""
    return "; ".join(_translate(part, _BASIS) or part for part in text.split("; ")) if text else text


def method_html_tr(classes, mmu_ha: float, resolution: float) -> str:
    rows = "".join(
        f"<tr><td><span class='chip' style='background:{c.color}'></span>{html.escape(CLASS_LABELS_TR.get(c.key, c.label))}</td><td><code>{html.escape(c.rule)}</code></td></tr>"
        for c in classes
    )
    mmu = tr_num(f"{mmu_ha:g}")
    res = tr_num(f"{resolution:g}")
    return f"""
<p>earthdiff, Sentinel-2 L2A yüzey yansımasının iki <b>medyan bileşimini</b> karşılaştırır. Bulut, bulut gölgesi, sirrus, kar ve doymuş pikseller
sahne sınıflandırma katmanıyla (SCL; bulut pikselleri 30&nbsp;m genişletilerek) ayıklanır, sonra piksel başına medyan alınır. Böylece her piksel,
o dönemdeki bulutsuz gözlemlerinin medyanıdır. Aşağıdakilerin hepsi açık ve incelenebilir kurallardır &ndash; makine öğrenmesi modeli yoktur.</p>
<table><thead><tr><th>Değişim sınıfı</th><th>Kural (d = sonra &minus; önce)</th></tr></thead><tbody>{rows}</tbody></table>
<pre>NDVI  = (NIR - Red) / (NIR + Red)                      B08, B04
MNDWI = (Green - SWIR1) / (Green + SWIR1)              B03, B11
BSI   = ((SWIR1 + Red) - (NIR + Blue)) / ((SWIR1 + Red) + (NIR + Blue))   B11, B04, B08, B02</pre>
<p>Piksel kurallarının sonucu temizlenir: 3 pikselden dar çıkıntılar morfolojik açma (3x3) ile silinir (20&nbsp;m ve daha küçük piksellerde),
<b>en küçük haritalama biriminden ({mmu}&nbsp;ha)</b> küçük lekeler atılır, kalanlar poligona çevrilir (8 komşuluk). Alanlar, UTM ızgarasında
piksel sayısı &times; piksel alanıdır ({res}&nbsp;m piksel).</p>
<p><b>Güç (güven puanı)</b> 0 ile 1 arasında sezgisel bir puandır, doğru olma olasılığı <i>değildir</i>: 0,5&times;büyüklük (ortalama indis değişiminin eşiği ne kadar aştığı, 2 katında doyar)
+ 0,3&times;geçerli gözlem (önce/sonra en küçüğü, 3'te doyar) + 0,2&times;boyut (alan / birim oranının log10'u, 10 katında doyar). &ge;0,70 &ldquo;güçlü&rdquo;, &ge;0,45 &ldquo;orta&rdquo;, altı &ldquo;zayıf&rdquo;.</p>
<h3>Olası neden, mevsim kontrolü, parlak yüzeyler</h3>
<p><b>Olası neden</b> (yalnız bitki kaybı poligonlarında) bir ipucudur, hüküm değildir: <code>fire-like</code> (yangına benzer) için dNBR en az 0,27 olmalı (Key &amp; Benson 2006'nın orta-düşük yanık şiddeti; Copernicus EFFIS de aynı eşikleri kullanır),
sonrasında yüzey koyu olmalı ve yangın lehine kanıt (düzensiz sınır gibi) açma lehine kanıttan fazla olmalı. <code>clearing-like</code> (açmaya benzer) zayıf yanık sinyali ya da düz kenarlı, parlak bir sınır demektir (ağaç kesimi, hasat, sürüm, inşaat); kalan her şey <code>unclear</code> (belirsiz).
Parlaklık ve şekil eşikleri bu projenin kendi sezgisidir. İki dönem yıllarca ayrıysa her bitki kaybı poligonu ara yılların birer geçişiyle de izlenir; bir yıldan ötekine NBR bütün dönemdekinden çok daha fazla düşmüşse (sonradan kısmen toparlanmış bir olay, örneğin eski bir yangın) ipucu o adıma göre verilir ve rapor olayın ne zaman olduğunu yazar. <b>Mevsimsel</b> işareti (isteğe bağlı kontrol), önceki yılların aynı dönemlerinde NDVI'ı benzer ölçüde düşen poligonlara konur; poligonlar hiçbir zaman silinmez.
<b>Parlak yüzeyler</b> (tuz, kum, kar: ortalama görünür yansıma &ge; 0,25 ve NDVI &lt; 0,15) görüntüyü doyurur; alanın büyük bölümünü kaplıyorsa rapor orada değişimin güvenilir olmadığını söyler ve üstlerindeki poligonların güven puanını düşürür.</p>
<h3>Sınırlar &mdash; lütfen okuyun</h3>
<ul>
<li><b>Mevsim ve tarım.</b> Ekin, çayır ve yaprak döken orman mevsime ve tarım işlerine göre renk değiştirir. Farklı yılların aynı mevsimini karşılaştırın; hasat, sürüm, sulama ve otlatma bitki kaybı ya da artışı gibi görünür.</li>
<li><b>Yanık şiddeti ya da orman hukuku ürünü değildir.</b> &ldquo;Bitki kaybı&rdquo; yeşilliğin azaldığını söyler; nedenini söylemez (yangın, kesim, kuraklık, hasat, inşaat). &ldquo;Yeni çıplak toprak / yapılaşma&rdquo; toprak nemi, kurumuş ekin ya da açığa çıkmış göl tabanı da olabilir.</li>
<li><b>Su.</b> Sığ, bulanık ya da yosunlu su ve ıslak toprak MNDWI kuralının iki tarafına da düşebilir; bulut gölgesi ve yamaç gölgesi suyu taklit edebilir.</li>
<li><b>Çözünürlük.</b> Burada Sentinel-2 pikselleri {res}&nbsp;m (SWIR bantları aslında 20&nbsp;m). Birkaç pikselden dar şeyler (yollar, küçük binalar, dar dereler) güvenilir biçimde bulunamaz. {mmu}&nbsp;ha'dan küçük her şey gizlenir.</li>
<li><b>Atmosfer ve geometri.</b> Kalan pus, tarihler arasındaki güneş ve bakış açısı farkı ve sahnelerin çakıştırması (~10&nbsp;m) kenarlarda yanlış alarm üretir.</li>
<li><b>Arazi doğrulaması yok.</b> Sonuçlar tarama ipuçlarıdır. Önemli bir bulguyu daha yüksek çözünürlüklü görüntü, arazi verisi ya da resmî kaynakla doğrulayın.</li>
</ul>"""


# ---------------------------------------------------------------- plain-language reading of a change (same texts as the report page)
CLASS_LABELS_EN = {
    "vegetation_loss": "Vegetation loss",
    "vegetation_gain": "Vegetation gain",
    "new_water": "New water",
    "water_loss": "Water loss",
    "new_bare_or_built": "New bare soil / built-up",
}
WHAT = {
    "en": {"vegetation_loss": "Green vegetation was lost here",
           "vegetation_gain": "Vegetation grew or became greener here (regrowth, new crops, planting)",
           "new_water": "Water appeared here (flooding, a filling reservoir or a new pond)",
           "water_loss": "Water disappeared here (a drying lake or reservoir, a retreating shore)",
           "new_bare_or_built": "The ground became bare or built-up here (quarry, construction, cleared or ploughed land)"},
    "tr": {"vegetation_loss": "Burada yeşil bitki örtüsü kaybolmuş",
           "vegetation_gain": "Burada bitki örtüsü artmış ya da yeşermiş (yeniden büyüme, yeni ekin, dikim)",
           "new_water": "Burada su ortaya çıkmış (taşkın, dolan baraj gölü ya da yeni gölet)",
           "water_loss": "Burada su çekilmiş (kuruyan göl ya da baraj, geri çekilen kıyı)",
           "new_bare_or_built": "Burada zemin çıplak toprağa ya da yapılaşmaya dönmüş (taş ocağı, inşaat, açılmış ya da sürülmüş arazi)"},
}
CAUSE = {
    "en": {"fire-like": "it looks like a burn: the surface is darker and burnt afterwards",
           "clearing-like": "it looks like clearing: felling, harvest, ploughing or construction", "unclear": "the cause is unclear"},
    "tr": {"fire-like": "yangın izine benziyor: sonrasında yüzey koyu ve yanık",
           "clearing-like": "açmaya benziyor: ağaç kesimi, hasat, sürüm ya da inşaat", "unclear": "nedeni belirsiz"},
}
CAUSE_SHORT = {"en": {"fire-like": "fire-like", "clearing-like": "clearing-like", "unclear": "unclear"},
               "tr": {"fire-like": "yangına benzer", "clearing-like": "açmaya benzer", "unclear": "belirsiz"}}
STRENGTH = {"en": {"high": "strong", "medium": "moderate", "low": "weak"}, "tr": {"high": "güçlü", "medium": "orta", "low": "zayıf"}}
SEASONAL_TXT = {"en": " The same drop happened at this time of year before, so it is probably harvest or a seasonal cycle.",
                "tr": " Aynı düşüş önceki yıllarda da yılın bu zamanında olmuş; muhtemelen hasat ya da mevsim döngüsü."}
EVENT_TXT = {"en": " The largest drop happened between {a} and {b}.", "tr": " En büyük düşüş {a} ile {b} arasında olmuş."}
BRIGHT_TXT = {"en": " Very bright surface: change here is unreliable.", "tr": " Çok parlak yüzey: buradaki değişim güvenilir değil."}


def class_label(key: str, lang: str) -> str:
    return (CLASS_LABELS_TR if lang == "tr" else CLASS_LABELS_EN).get(key, key)


def describe(p: dict, lang: str = "en") -> str:
    """One plain sentence on what changed at a feature (``p`` = its properties)."""
    lang = "tr" if lang == "tr" else "en"
    s = WHAT[lang].get(p.get("class"), class_label(p.get("class", ""), lang))
    if p.get("class") == "vegetation_loss" and p.get("likely_cause") in CAUSE[lang]:
        s += "; " + CAUSE[lang][p["likely_cause"]]
    s += "."
    if p.get("likely_seasonal") is True:
        s += SEASONAL_TXT[lang]
    if p.get("event_before") and p.get("event_after"):
        s += EVENT_TXT[lang].format(a=p["event_before"], b=p["event_after"])
    if (p.get("bright_share") or 0) > 0.2:
        s += BRIGHT_TXT[lang]
    return s
