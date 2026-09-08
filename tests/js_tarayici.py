"""Panelin gömülü JavaScript'i için sözdizimi nöbetçisi.

NEDEN VAR: 2026-09-08'de `alert("...` içindeki `\\n` gerçek satır sonuna
dönüştü. JS'te çift tırnaklı dizgi satır atlayamaz — tek bir sözdizimi hatası
TÜM script'i çalıştırmaz. Sonuç: panel açılıyor, başlıklar görünüyor, ama
hiçbir tablo dolmuyor. Kullanıcı "panel kapalı" sandı; sunucu gayet ayaktaydı.

30 kritik testin hepsi geçmişti, çünkü hiçbiri panelin JS'ine bakmıyordu.
Otomatik dağıtım da bu yüzden bozuk sürümü canlıya taşıdı. Bu dosya o boşluk.

`node --check` varsa GERÇEK ayrıştırıcı kullanılır (en güvenilir yol). Yoksa
aşağıdaki tarayıcı devreye girer: JS'i karakter karakter yürür ve tırnaklı bir
dizginin içinde ham satır sonu görürse hata verir. Şablon dizgileri (backtick)
satır atlayabildiği için onlar serbest bırakılır; `${...}` içine girip çıkmayı
ve iç içe şablonları da takip eder.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

# Bir '/' karakterinden ÖNCE bunlar geliyorsa bölme değil, düzenli ifade başlar.
REGEX_ONCESI = set("(,=:[!&|?{};+-*%~^<>") | {""}


def _script_cikar(sayfa: str) -> str:
    return sayfa.split("<script>", 1)[1].rsplit("</script>", 1)[0]


def ham_satir_sonu_ara(js: str) -> list[str]:
    """Tırnaklı dizgi içinde ham satır sonu olan yerleri döndürür."""
    hatalar: list[str] = []
    i, n, satir = 0, len(js), 1
    # yigin: şablon dizgisi içinde ${...} takibi için
    yigin: list[str] = []
    onceki = ""
    while i < n:
        c = js[i]
        if c == "\n":
            satir += 1
            i += 1
            continue
        if c in " \t":
            i += 1
            continue
        # yorumlar
        if c == "/" and i + 1 < n and js[i + 1] == "/":
            while i < n and js[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and js[i + 1] == "*":
            i += 2
            while i + 1 < n and not (js[i] == "*" and js[i + 1] == "/"):
                satir += js[i] == "\n"
                i += 1
            i += 2
            continue
        # düzenli ifade
        if c == "/" and onceki in REGEX_ONCESI:
            i += 1
            while i < n and js[i] != "/":
                if js[i] == "\\":
                    i += 1
                elif js[i] == "[":            # sınıf içinde '/' kaçmayabilir
                    while i < n and js[i] != "]":
                        i += 2 if js[i] == "\\" else 1
                i += 1
            i += 1
            onceki = "/"
            continue
        # tırnaklı dizgiler — SATIR ATLAYAMAZ
        if c in "'\"":
            basladi, i = satir, i + 1
            while i < n and js[i] != c:
                if js[i] == "\\":
                    i += 1
                elif js[i] == "\n":
                    hatalar.append(
                        f"satır {basladi}: {c} ile açılan dizgi satır sonuna kadar "
                        f"kapanmamış — JS'te çift/tek tırnaklı dizgi satır atlayamaz "
                        f"(şablon için backtick kullan ya da \\\\n yaz)")
                    break
                i += 1
            i += 1
            onceki = "x"
            continue
        # şablon dizgisi — satır atlayabilir, ${} içine inebilir
        if c == "`":
            yigin.append("`")
            i += 1
            while i < n and yigin:
                if js[i] == "\\":
                    i += 2
                    continue
                if yigin[-1] == "`":
                    if js[i] == "`":
                        yigin.pop()
                    elif js[i] == "$" and i + 1 < n and js[i + 1] == "{":
                        yigin.append("{")
                        i += 1
                else:                          # ${...} içindeyiz
                    if js[i] == "`":
                        yigin.append("`")
                    elif js[i] == "{":
                        yigin.append("{")
                    elif js[i] == "}":
                        yigin.pop()
                satir += js[i] == "\n"
                i += 1
            onceki = "x"
            continue
        onceki = c
        i += 1
    return hatalar


def dogrula(sayfa: str) -> list[str]:
    """Sayfanın JS'ini doğrular. Boş liste = sorun yok."""
    js = _script_cikar(sayfa)
    if shutil.which("node"):
        yol = Path(tempfile.mkdtemp()) / "panel.js"
        yol.write_text(js, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(yol)],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return [f"node --check başarısız:\n{(r.stderr or r.stdout).strip()}"]
        return []
    return ham_satir_sonu_ara(js)
