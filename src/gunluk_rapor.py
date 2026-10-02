"""AKŞAM ÖZETİ — günde BİR mesaj, üç kanal birden.

NEDEN (2026-10-03): kullanıcı "çok uyarı alıyorum, akşamdan akşama sonuca
bakalım" dedi. Üç kanal + 20 parite ile her açılış/kapanış/R eşiği
bildirilince günde 20'yi aşan mesaj oluyordu. Rutin bildirimler kapatıldı
(SCALP_BILDIRIM=false, R_NOTIFY_LEVEL=0) ve sonuçlar buraya toplandı.

SESSİZLİK ARIZAYI GİZLEMEK İÇİN KULLANILMAZ — bu projenin en pahalı dersi.
Şunlar HER ZAMAN anında gider ve bu modül onları etkilemez:
  • HATA bildirimleri (döngü hatası, broker arızası, mutabakat)
  • ACİL durumlar (stopsuz açık pozisyon)
  • Bot başlatıldı / dağıtım uyarıları
  • Telegram kanalının kendi sağlığı (panelde kırmızı bant)
Kapatılan tek şey RUTİN iş akışı: "pozisyon açıldı", "pozisyon kapandı".

Rapor her kanalı AYRI gösterir ve karneler pozisyon bazlı hesaplanır
(kısmi kapanışlar tek pozisyon sayılır — bkz. state.pnl_pozisyon_bazli).
Borsa karnesi para birimi başına ayrılır; dolar ve lira toplanmaz.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .performans import ozet
from .state import StateStore

log = logging.getLogger("rapor")

DAMGA = "son_aksam_raporu"


def _kanal_satiri(ad: str, varlik: float, baslangic: float,
                  pnl: list[float], acik: int, birim: str = "USDT") -> str:
    o = ozet(pnl)
    yuzde = (varlik / baslangic - 1) * 100 if baslangic else 0.0
    s = (f"*{ad}* — `{varlik:,.2f}` {birim} (`{yuzde:+.2f}%`)\n"
         f"  açık {acik} · {o['n']} pozisyon · kazanma `%{o['kazanma_orani']}`")
    if o["n"]:
        s += (f"\n  ödeme `{o['odeme_orani']:.2f}×` · beklenti "
              f"`{o['beklenti']:+,.2f}` · toplam `{o['toplam']:+,.2f}`")
    return s


def gunluk_ozet_metni(cfg) -> str:
    """Üç kanalın akşam özeti. Panel/bot dışından da çağrılabilir (teşhis)."""
    bolum = [f"🌙 *AKŞAM ÖZETİ* · {datetime.now().strftime('%d.%m.%Y %H:%M')}"]

    # --- KRİPTO (ana kanal)
    try:
        st = StateStore(cfg.db_path)
        bakiye = float(st.get_kv("fut_usdt", "10000.0"))
        varlik = bakiye + sum(p.margin for p in st.all_positions())
        bolum.append(_kanal_satiri("KRİPTO (ana)", varlik, 10_000.0,
                                   st.pnl_pozisyon_bazli(),
                                   len(st.all_positions())))
    except Exception as e:  # noqa: BLE001
        bolum.append(f"*KRİPTO* — özet alınamadı: {str(e)[:60]}")

    # --- GENİŞ kanal (eski adı scalp)
    if cfg.scalp_enabled:
        try:
            sc = StateStore(cfg.scalp_db_path)
            bak = float(sc.get_kv("scalp_usdt", str(cfg.scalp_baslangic_usdt)))
            var = bak + sum(p.margin for p in sc.all_positions())
            bolum.append(_kanal_satiri(
                f"GENİŞ ({cfg.scalp_timeframe}, {len(cfg.scalp_symbols)} parite)",
                var, cfg.scalp_baslangic_usdt, sc.pnl_pozisyon_bazli(),
                len(sc.all_positions())))
        except Exception as e:  # noqa: BLE001
            bolum.append(f"*GENİŞ* — özet alınamadı: {str(e)[:60]}")

    # --- BORSA: cüzdan başına AYRI (dolar ve lira toplanmaz)
    if cfg.borsa_enabled:
        try:
            bo = StateStore(cfg.borsa_db_path)
            from .borsa_data import para_birimi
            kova: dict[str, list[float]] = {}
            for sembol, pnl in bo.pozisyon_pnl_sembollu():
                kova.setdefault(para_birimi(sembol), []).append(pnl)
            acik = len(bo.all_positions())
            satir = ["*BORSA* (sanal)"]
            for cur, v in sorted(kova.items()):
                o = ozet(v)
                satir.append(f"  {cur}: {o['n']} pozisyon · kazanma "
                             f"`%{o['kazanma_orani']}` · toplam `{o['toplam']:+,.2f}`")
            if not kova:
                satir.append("  _henüz kapanmış pozisyon yok_")
            satir.append(f"  açık {acik}")
            bolum.append("\n".join(satir))
        except Exception as e:  # noqa: BLE001
            bolum.append(f"*BORSA* — özet alınamadı: {str(e)[:60]}")

    bolum.append("_Rutin açılış/kapanış bildirimleri kapalı. "
                 "Hata ve acil durumlar her zaman anında gelir._")
    return "\n\n".join(bolum)


def belki_gonder(cfg, state: StateStore, notifier) -> bool:
    """Rapor saati geldiyse ve bugün gönderilmediyse gönderir.

    Damga ANA kanalın DB'sinde tutulur: üç kanal ayrı DB kullanıyor ama
    rapor tek, dolayısıyla "gönderildi" bilgisi de tek yerde olmalı —
    yoksa kanal sayısı kadar rapor gider.
    """
    simdi = datetime.now()               # rapor saati YERELdir (akşam = akşam)
    bugun = simdi.date().isoformat()
    if simdi.hour < cfg.daily_report_hour or state.get_kv(DAMGA) == bugun:
        return False
    # Damgayı ÖNCE yaz: gönderim hata verse bile aynı akşam tekrar tekrar
    # denenip sel üretmesin. Hata zaten notifier tarafından loglanır.
    state.set_kv(DAMGA, bugun)
    try:
        metin = gunluk_ozet_metni(cfg)
    except Exception as e:  # noqa: BLE001
        log.error("Akşam özeti hazırlanamadı: %s", e, exc_info=True)
        return False
    notifier.send(metin)
    log.info("Akşam özeti gönderildi (%s)", bugun)
    return True
