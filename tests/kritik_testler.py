"""KRİTİK TESTLER — otomatik dağıtımın güvenlik kapısı.

Bu dosya `deploy/sunucu-otomatik-guncelle.sh` tarafından, botu YENİDEN
BAŞLATMADAN ÖNCE çalıştırılır. Çıkış kodu 0 değilse dağıtım DURUR ve
Telegram'a uyarı gider. Yani buradaki her test, "canlı bota bu hatayla
gitmesin" dediğimiz bir şeyi koruyor.

Neden gerekli: 2026-09-07 oturumunda manuel stop kilidinin açılma kuralı
YANLIŞ yazılmıştı (1 dolarlık yeni tepe kullanıcının iznini iptal ediyordu).
Hatayı yalnızca test yakaladı. O test geçici bir dosyadaydı ve atılmıştı —
bu dosya o dersin sonucudur.

Kapsam (gerçek DB'ye DOKUNMAZ, geçici dosya kullanır):
  - Geriye uyumluluk: yeni alanları olmayan eski pozisyon kayıtları
  - Eşzamanlı pozisyon tavanı (otomatik ret / manuel geçiş + uyarı)
  - 4R kâr bildirimi (bildirir ama ASLA kapatmaz, spam yapmaz)
  - Manuel stop: geçersiz girişlerin reddi, sıkma, gevşetme kilidi
  - SHORT tarafı
  - Komut kuyruğu (panel → bot tek yazıcı akışı)
  - Komut SONUCU: her komut ok/red/bilgi izi bırakır (sessiz yutma yok)
  - Panel JS: gömülü script ayrışıyor mu, aradığı id'ler var mı
  - Zaman: damgalar ofsetli gidiyor mu (kırpılırsa 3 saat sessizce kayar)
  - Dağıtım teşhisi: /api/deploy-durum çökmeden durum veriyor mu
  - Dağıtım gerilik alarmı: döngü ölünce panel kırmızıya dönüyor mu

    python -m tests.kritik_testler
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import CONFIG  # noqa: E402
from src.futures_trader import FuturesLiveTrader, FuturesPaperTrader  # noqa: E402
from src.state import Position, StateStore  # noqa: E402

GECEN, KALAN = 0, []


def ok(baslik: str) -> None:
    global GECEN
    GECEN += 1
    print(f"  {GECEN:2d}) {baslik:<62s} ✔")


def kur():
    """Her test grubu için taze, İZOLE bir bot örneği."""
    db = Path(tempfile.mkdtemp()) / "test.db"
    state = StateStore(db)
    market = MagicMock()
    market.filters.return_value = MagicMock(step_size=0.001, min_qty=0.001,
                                            tick_size=0.01, min_notional=5)
    market.last_price.return_value = 100.0
    breaker = MagicMock()
    breaker.entries_allowed.return_value = (True, 0.0)
    notifier = MagicMock()
    cfg = replace(CONFIG, max_concurrent_positions=4, r_notify_level=4.0)
    return state, notifier, FuturesPaperTrader("SOLUSDT", cfg, market, state,
                                               notifier, breaker), db


def poz(state, sym="SOLUSDT", entry=100.0, stop=109.0, side="LONG",
        high=112.0, qty=10.0, risk_unit=3.0):
    state.clear_position(sym)
    p = Position(symbol=sym, qty=qty, entry_price=entry, highest_price=high,
                 trailing_stop=stop, stop_order_id=None,
                 entry_time=datetime.now(timezone.utc).isoformat(),
                 entry_fee_usdt=0.5, side=side, margin=500.0, funding_acc=0.0,
                 risk_unit=risk_unit)
    state.save_position(p)
    return p


def mum(high, low, atr=1.0):
    return pd.Series({"high": high, "low": low, "atr": atr})


# ------------------------------------------------------------ geriye uyumluluk
def test_geriye_uyumluluk():
    print("\nGERİYE UYUMLULUK")
    state, _, _, db = kur()
    # Eski kayıtta risk_unit / r_notified / stop_manual_ref YOK.
    eski = {"symbol": "OLDUSDT", "qty": 5.0, "entry_price": 50.0,
            "highest_price": 55.0, "trailing_stop": 48.0, "stop_order_id": None,
            "entry_time": "2026-09-06T04:00:00+00:00", "entry_fee_usdt": 0.3,
            "side": "LONG", "margin": 250.0, "funding_acc": 0.1}
    c = sqlite3.connect(str(db))
    c.execute("INSERT INTO positions(symbol, data) VALUES(?,?)",
              ("OLDUSDT", json.dumps(eski)))
    c.commit()
    c.close()
    p = state.get_position("OLDUSDT")
    assert p is not None, "eski format yüklenemedi"
    assert p.entry_price == 50.0 and p.trailing_stop == 48.0, "eski veri bozuldu"
    assert p.risk_unit == 0.0 and p.r_notified == 0.0 and p.stop_manual_ref == 0.0
    ok("yeni alanları olmayan eski pozisyon bozulmadan yüklendi")


# ----------------------------------------------------------- pozisyon tavanı
def test_pozisyon_tavani():
    print("\nEŞZAMANLI POZİSYON TAVANI")
    state, notifier, t, _ = kur()
    for s in ("BTCUSDT", "ETHUSDT", "BNBUSDT", "DOTUSDT"):
        poz(state, s)
    notifier.reset_mock()
    t._open("LONG", atr=1.0, reason="test")
    assert state.get_position("SOLUSDT") is None, "tavan dolu ama pozisyon açıldı"
    ok("tavan dolu (4/4) → otomatik giriş reddedildi")

    notifier.reset_mock()
    t._open("LONG", atr=1.0, manual=True)
    assert state.get_position("SOLUSDT") is not None, "manuel giriş engellenmemeliydi"
    assert notifier.send_error.called, "manuel girişte uyarı gitmeliydi"
    assert "tavan" in notifier.send_error.call_args[0][0].lower()
    ok("tavan dolu + MANUEL → girildi ama uyarı gönderildi")

    state.clear_position("SOLUSDT")
    state.clear_position("DOTUSDT")
    t._open("LONG", atr=1.0, reason="test")
    yeni = state.get_position("SOLUSDT")
    assert yeni is not None and yeni.risk_unit > 0, "risk_unit kaydedilmedi"
    ok("tavan altında (3/4) → girildi ve risk_unit kaydedildi")


# --------------------------------------------------------------- 4R bildirimi
def test_r_bildirimi():
    print("\n4R KÂR BİLDİRİMİ (bildirir, ASLA kapatmaz)")
    state, notifier, t, _ = kur()
    poz(state, entry=100.0, stop=97.0, high=100.0)      # 1R = 3$

    notifier.reset_mock()
    t._check_r_notify(state.get_position("SOLUSDT"), 111.6)   # 3.87R
    assert not notifier.send.called, "4R'ye ulaşmadan bildirim gitti"
    ok("3.9R → bildirim yok")

    p = state.get_position("SOLUSDT")
    p.highest_price, p.trailing_stop = 112.5, 109.5
    state.save_position(p)
    notifier.reset_mock()
    t._check_r_notify(state.get_position("SOLUSDT"), 112.5)   # 4.17R
    assert notifier.send.called, "4R'de bildirim gitmedi"
    assert "DEVAM" in notifier.send.call_args[0][0]
    ok("4.2R → bildirim gönderildi")

    assert state.get_position("SOLUSDT") is not None, "bildirim pozisyonu KAPATTI"
    ok("bildirim sonrası pozisyon hâlâ açık (kritik)")

    notifier.reset_mock()
    t._check_r_notify(state.get_position("SOLUSDT"), 113.0)
    assert not notifier.send.called, "aynı seviyede tekrar bildirim (spam)"
    ok("aynı seviyede tekrar → bildirim yok (spam koruması)")

    p = state.get_position("SOLUSDT")
    p.highest_price, p.trailing_stop = 125.0, 122.0
    state.save_position(p)
    notifier.reset_mock()
    t._check_r_notify(state.get_position("SOLUSDT"), 125.0)   # 8.33R
    assert notifier.send.called, "8R'de yeni bildirim gitmedi"
    ok("8.3R → yeni eşikte tekrar bildirim")

    # risk_unit'i olmayan eski pozisyon → yaklaşık hesap
    state.clear_position("SOLUSDT")
    poz(state, entry=100.0, stop=109.0, high=112.0, risk_unit=0.0)
    notifier.reset_mock()
    t._check_r_notify(state.get_position("SOLUSDT"), 112.0)
    assert notifier.send.called and "≈" in notifier.send.call_args[0][0]
    assert state.get_position("SOLUSDT").risk_unit == 3.0
    ok("risk_unit'i olmayan eski pozisyon → yaklaşık (≈) hesaplandı")

    assert len(state.recent_r_events(10)) >= 3, "r_events tablosu boş"
    ok("r_events tablosuna kayıt yazıldı")


# --------------------------------------------------------------- manuel stop
def test_manuel_stop_ret():
    print("\nMANUEL STOP — reddedilmesi gerekenler")
    state, notifier, t, _ = kur()
    poz(state)

    notifier.reset_mock()
    t._set_manual_stop(state.get_position("SOLUSDT"), 111.0, "111.5")
    assert state.get_position("SOLUSDT").trailing_stop == 109.0
    assert notifier.send_error.called and "KAPAT" in notifier.send_error.call_args[0][0]
    ok("LONG: stop ≥ anlık fiyat → reddedildi, KAPAT'a yönlendirdi")

    notifier.reset_mock()
    t._set_manual_stop(state.get_position("SOLUSDT"), 111.0, "0")
    assert state.get_position("SOLUSDT").trailing_stop == 109.0
    assert notifier.send_error.called
    ok("stop = 0 → reddedildi ('stop loss daima olacak')")

    t._set_manual_stop(state.get_position("SOLUSDT"), 111.0, "abc")
    assert state.get_position("SOLUSDT").trailing_stop == 109.0
    ok("bozuk sayı → yok sayıldı, pozisyon bozulmadı")


def test_manuel_stop_sikma():
    print("\nMANUEL STOP — sıkma (cırcır doğal olarak korur)")
    state, notifier, t, _ = kur()
    poz(state)
    t._set_manual_stop(state.get_position("SOLUSDT"), 112.0, "110.5")
    p = state.get_position("SOLUSDT")
    assert p.trailing_stop == 110.5 and p.stop_manual_ref == 0.0
    ok("stop 109 → 110.5 sıkıldı, kilit kurulmadı")

    t._update_trailing(state.get_position("SOLUSDT"), mum(112.0, 108.0))
    assert state.get_position("SOLUSDT").trailing_stop == 110.5, "algoritma sıkmayı geri aldı"
    ok("sonraki mum: aday 109 < 110.5 → bot manuel seviyeyi korudu")

    t._update_trailing(state.get_position("SOLUSDT"), mum(120.0, 115.0))
    assert abs(state.get_position("SOLUSDT").trailing_stop - 117.0) < 1e-9
    ok("fiyat 120 → stop 117'ye normal şekilde yükseldi")


def test_manuel_stop_gevsetme():
    print("\nMANUEL STOP — gevşetme kilidi (kilit olmasa bot geri alırdı)")
    state, notifier, t, _ = kur()
    poz(state)
    notifier.reset_mock()
    t._set_manual_stop(state.get_position("SOLUSDT"), 112.0, "105")
    p = state.get_position("SOLUSDT")
    # ref = 2×eski − yeni = 2×109 − 105 = 113 (verilen 4$'lık pay geri kazanılmalı)
    assert p.trailing_stop == 105.0 and p.stop_manual_ref == 113.0, p.stop_manual_ref
    assert notifier.send.called and "GEVŞETİLDİ" in notifier.send.call_args[0][0]
    ok("stop 109 → 105 gevşetildi, kilit ref = 113 (109 + 4 pay)")

    t._update_trailing(state.get_position("SOLUSDT"), mum(112.0, 108.0))
    assert state.get_position("SOLUSDT").trailing_stop == 105.0, "bot gevşetmeyi geri aldı"
    ok("sonraki mum: aday 109 ≤ ref 113 → stop 105'te kaldı")

    # REGRESYON: ilk tasarımda 1 dolarlık yeni tepe izni iptal ediyordu.
    t._update_trailing(state.get_position("SOLUSDT"), mum(113.0, 108.0))
    p = state.get_position("SOLUSDT")
    assert p.highest_price == 113.0, "uç değer güncellenmedi"
    assert p.trailing_stop == 105.0, "1$'lık yeni tepe manuel izni İPTAL ETTİ"
    ok("1$ yeni tepe (aday 110 ≤ 113) → izin korundu [regresyon]")

    t._update_trailing(state.get_position("SOLUSDT"), mum(125.0, 120.0))
    p = state.get_position("SOLUSDT")
    assert p.stop_manual_ref == 0.0, "kilit açılmadı"
    assert abs(p.trailing_stop - 122.0) < 1e-9
    ok("fiyat 125 → aday 122 > ref 113 → kilit açıldı, stop 122")


def test_short():
    print("\nSHORT TARAFI")
    state, notifier, t, _ = kur()
    poz(state, entry=100.0, stop=91.0, side="SHORT", high=88.0)
    t._set_manual_stop(state.get_position("SOLUSDT"), 88.0, "89.5")
    assert state.get_position("SOLUSDT").trailing_stop == 89.5
    ok("SHORT stop 91 → 89.5 sıkıldı (aşağı = sıkma)")

    notifier.reset_mock()
    t._set_manual_stop(state.get_position("SOLUSDT"), 88.0, "87.0")
    assert state.get_position("SOLUSDT").trailing_stop == 89.5
    assert notifier.send_error.called
    ok("SHORT: stop ≤ anlık fiyat → reddedildi")

    poz(state, entry=100.0, stop=91.0, side="SHORT", high=88.0)
    t._set_manual_stop(state.get_position("SOLUSDT"), 88.0, "95")
    p = state.get_position("SOLUSDT")
    assert p.trailing_stop == 95.0 and p.stop_manual_ref == 87.0   # 2×91−95
    t._update_trailing(state.get_position("SOLUSDT"), mum(89.0, 86.0))
    assert state.get_position("SOLUSDT").trailing_stop == 95.0
    ok("SHORT gevşetme kilidi tutuyor (aday 89 ≥ ref 87)")

    t._update_trailing(state.get_position("SOLUSDT"), mum(81.0, 80.0))
    p = state.get_position("SOLUSDT")
    assert p.stop_manual_ref == 0.0 and abs(p.trailing_stop - 83.0) < 1e-9
    ok("SHORT fiyat 80 → aday 83 < ref 87 → kilit açıldı, stop 83")

    poz(state, entry=100.0, stop=91.0, side="SHORT", high=88.0)
    t._set_manual_stop(state.get_position("SOLUSDT"), 88.0, "500")
    assert state.get_position("SOLUSDT").stop_manual_ref > 0, "ref≤0 nöbetçisi çalışmadı"
    ok("aşırı gevşetme: ref hesabı ≤0 olsa da kilit kuruldu")

    poz(state, entry=100.0, stop=103.0, side="SHORT", high=88.0)
    p = state.get_position("SOLUSDT")
    p.trailing_stop = 91.0
    state.save_position(p)
    notifier.reset_mock()
    t._check_r_notify(state.get_position("SOLUSDT"), 88.0)   # (100−88)/3 = 4R
    assert notifier.send.called, "SHORT'ta 4R bildirimi gitmedi"
    ok("SHORT pozisyonda 4R bildirimi çalışıyor")


def test_komut_kuyrugu():
    print("\nKOMUT KUYRUĞU (panel → bot, tek yazıcı)")
    state, _, t, _ = kur()
    poz(state)
    state.set_kv("cmd_SOLUSDT", "STOP:106.25")
    t._process_manual_commands(state.get_position("SOLUSDT"), 112.0)
    assert state.get_position("SOLUSDT").trailing_stop == 106.25
    assert state.get_kv("cmd_SOLUSDT") == "", "komut tüketilmedi (tekrar işlenir)"
    ok("'STOP:106.25' uygulandı ve kuyruktan tüketildi")

    state.clear_position("SOLUSDT")
    state.set_kv("cmd_SOLUSDT", "STOP:50")
    assert t._process_manual_commands(None, 112.0) is None
    ok("açık pozisyon yokken STOP komutu güvenle yok sayıldı")

    poz(state)
    state.set_kv("cmd_SOLUSDT", "CLOSE")
    assert t._process_manual_commands(state.get_position("SOLUSDT"), 112.0) is None
    assert state.get_position("SOLUSDT") is None, "CLOSE pozisyonu kapatmadı"
    ok("CLOSE komutu bozulmadı [regresyon]")


def test_komut_sonucu():
    """Her komut bir iz bırakmalı — 'sessizce yutuldu' hâli KALMAMALI.

    Bot komutu kabul etse de reddetse de kuyruktan siliyor. Sonuç yazılmazsa
    panelin ⏳ rozeti söner ve kullanıcı bunu 'uygulandı' sanar. Bu test tam
    olarak o yanlış izlenimin geri gelmesini engeller.
    """
    print("\nKOMUT SONUCU (panelin ⏳ → ✅/⛔ şeridi)")

    def sonuc(state):
        ham = state.get_kv("cmdres_SOLUSDT", "")
        durum, _, kalan = ham.partition("|")
        ts, _, mesaj = kalan.partition("|")
        datetime.fromisoformat(ts)          # panel bunu ayrıştırabilmeli
        return durum, mesaj

    state, _, t, _ = kur()
    poz(state)
    state.set_kv("cmd_SOLUSDT", "STOP:106.25")
    t._process_manual_commands(state.get_position("SOLUSDT"), 112.0)
    durum, mesaj = sonuc(state)
    assert durum == "ok" and "106" in mesaj, (durum, mesaj)
    ok("stop sıkma → 'ok' + seviye mesajı")

    state.set_kv("cmdres_SOLUSDT", "")
    state.set_kv("cmd_SOLUSDT", "STOP:113")
    t._process_manual_commands(state.get_position("SOLUSDT"), 112.0)
    durum, mesaj = sonuc(state)
    assert durum == "red" and "KAPAT" in mesaj, (durum, mesaj)
    assert state.get_position("SOLUSDT").trailing_stop == 106.25
    ok("anında tetikleyen stop → 'red' + sebep (stop değişmedi)")

    state.set_kv("cmdres_SOLUSDT", "")
    state.set_kv("cmd_SOLUSDT", "STOP:106.25")
    t._process_manual_commands(state.get_position("SOLUSDT"), 112.0)
    durum, _ = sonuc(state)
    assert durum == "bilgi", durum
    ok("aynı seviye tekrar → 'bilgi' (sessizce yutulmuyor)")

    state.set_kv("cmdres_SOLUSDT", "")
    state.clear_position("SOLUSDT")
    state.set_kv("cmd_SOLUSDT", "STOP:50")
    t._process_manual_commands(None, 112.0)
    durum, _ = sonuc(state)
    assert durum == "red", durum
    ok("pozisyon yokken STOP → 'red' (eskiden tamamen sessizdi)")

    state.set_kv("cmdres_SOLUSDT", "")
    state.set_kv("cmd_SOLUSDT", "CLOSE")
    t._process_manual_commands(None, 112.0)
    durum, _ = sonuc(state)
    assert durum == "red", durum
    ok("pozisyon yokken CLOSE → 'red'")

    state.set_kv("cmdres_SOLUSDT", "")
    poz(state)
    state.set_kv("cmd_SOLUSDT", "CLOSE")
    t._process_manual_commands(state.get_position("SOLUSDT"), 112.0)
    durum, _ = sonuc(state)
    assert durum == "ok" and state.get_position("SOLUSDT") is None
    ok("başarılı CLOSE → 'ok' (satır kaybolsa da sonuç şeritte kalır)")

    state.set_kv("cmdres_SOLUSDT", "")
    state.set_kv("cmd_SOLUSDT", "SACMALIK")
    t._process_manual_commands(None, 112.0)
    durum, _ = sonuc(state)
    assert durum == "red", durum
    ok("bilinmeyen komut → 'red' (eskiden hiç iz bırakmazdı)")

    state.set_kv("cmdres_SOLUSDT", "")
    poz(state)
    state.set_kv("cmd_SOLUSDT", "OPEN_LONG")
    t._process_manual_commands(state.get_position("SOLUSDT"), 112.0)
    durum, _ = sonuc(state)
    assert durum == "red", durum
    ok("zaten pozisyon varken AÇ → 'red'")


def test_panel_komut_seridi():
    """Panelin şeridi: kuyruktakini 'bekliyor', sonucu TTL boyunca gösterir."""
    print("\nPANEL KOMUT ŞERİDİ")
    from src import panel

    state, _, t, _ = kur()
    sym = CONFIG.symbols[0]
    state.set_kv(f"cmd_{sym}", "STOP:1.23")
    with patch.object(panel, "state", state), \
         patch.object(panel, "CONFIG", replace(CONFIG, symbols=[sym])):
        (satir,) = panel.build_komutlar()
        assert satir["durum"] == "bekliyor" and satir["symbol"] == sym
        ok("kuyrukta komut varken → 'bekliyor'")

        # Sonuç geldi, komut tüketildi.
        state.set_kv(f"cmd_{sym}", "")
        state.set_kv(f"cmdres_{sym}",
                     f"red|{datetime.now(timezone.utc).isoformat()}|Anında tetikler")
        (satir,) = panel.build_komutlar()
        assert satir["durum"] == "red" and satir["mesaj"] == "Anında tetikler"
        ok("komut tüketilince → sonuç ('red' + sebep) gösteriliyor")

        # Kuyrukta yeni komut varsa ESKİ sonuç değil, 'bekliyor' görünmeli.
        state.set_kv(f"cmd_{sym}", "CLOSE")
        (satir,) = panel.build_komutlar()
        assert satir["durum"] == "bekliyor", "bayat sonuç yeni komutun üstünü örttü"
        ok("yeni komut kuyruğa girince bayat sonuç gösterilmiyor")

        # TTL dolunca şeritten düşer (panel sonsuza kadar kalabalık olmasın).
        state.set_kv(f"cmd_{sym}", "")
        eski = datetime.now(timezone.utc) - timedelta(seconds=panel.CMD_RESULT_TTL_S + 30)
        state.set_kv(f"cmdres_{sym}", f"ok|{eski.isoformat()}|Uygulandı")
        assert panel.build_komutlar() == []
        ok(f"{panel.CMD_RESULT_TTL_S} sn sonra sonuç şeritten düşüyor")

        # Bozuk kayıt paneli ÇÖKERTMEMELİ.
        state.set_kv(f"cmdres_{sym}", "ok|bozuk-zaman|x")
        assert panel.build_komutlar() == []
        ok("bozuk zaman damgası → satır atlanıyor, panel çökmüyor")


def test_panel_js():
    """Panelin gömülü JS'i AYRIŞMALI ve baktığı her id HTML'de OLMALI.

    2026-09-08: `alert("...` içindeki \\n gerçek satır sonuna dönüştü. JS'te
    çift tırnaklı dizgi satır atlayamaz; tek sözdizimi hatası TÜM script'i
    öldürdü. Panel açıldı, başlıklar göründü, hiçbir tablo dolmadı — dışarıdan
    "panel kapalı" gibi. 30 testin hepsi geçmişti çünkü hiçbiri JS'e bakmıyordu
    ve otomatik dağıtım bozuk sürümü canlıya taşıdı. Bu test o boşluğu kapatır.
    """
    print("\nPANEL JS (dağıtım kapısının kör noktasıydı)")
    import re

    from src.panel import PAGE
    from tests.js_tarayici import _script_cikar, dogrula, ham_satir_sonu_ara

    hatalar = dogrula(PAGE)
    assert not hatalar, "panel JS ayrıştırılamıyor:\n" + "\n".join(hatalar)
    ok("gömülü JS sözdizimi geçerli")

    # Yedek tarayıcı, node'suz sunucuda tek savunma — o da temiz demeli.
    assert not ham_satir_sonu_ara(_script_cikar(PAGE)), "yedek tarayıcı yanlış alarm verdi"
    ok("node'suz yedek tarayıcı da temiz (yanlış alarm yok)")

    # REGRESYON: bugünkü hatanın aynısını geri koy, yakalanmalı.
    bozuk = PAGE.replace("alert(`Komut", 'alert("Komut').replace("dakika.`);", 'dakika.");')
    assert bozuk != PAGE, "regresyon örneği kurulamadı (metin değişmiş)"
    assert ham_satir_sonu_ara(_script_cikar(bozuk)), "tarayıcı bugünkü hatayı KAÇIRDI"
    ok("aynı hata geri konsa yakalanıyor [regresyon]")

    # Runtime tarafı: $("x") ile aranan her id sayfada tanımlı olmalı.
    js = _script_cikar(PAGE)
    istenen = set(re.findall(r'\$\("([^"]+)"\)', js))
    tanimli = set(re.findall(r'id="([^"]+)"', PAGE))
    eksik = istenen - tanimli
    assert not eksik, f"JS'in aradığı id HTML'de yok: {sorted(eksik)}"
    ok(f"JS'in baktığı {len(istenen)} id'nin hepsi HTML'de tanımlı")

    # Şeridin kabı gerçekten var mı (yeni özellik boşa düşmesin).
    assert 'id="komutlar"' in PAGE and "cmdrow" in PAGE
    ok("komut şeridinin kabı ve stili sayfada")


def test_panel_saat():
    """Zaman damgaları TAM ISO (ofsetli) gitmeli; biçimlemeyi tarayıcı yapar.

    Panel "21:35 UTC" gösteriyordu, kullanıcı saatine bakıp 00:35 görüyordu ve
    aradaki 3 saati kafadan ekliyordu. Sunucu saati DOĞRUYDU (ölçtüm: −2 sn),
    sunum yanlıştı.

    Kritik nokta: dizgi kırpılıp ofset düşerse (eskiden `entry_time[:16]`
    yapılıyordu) `new Date("2026-09-06 04:00")` bunu YEREL saat sanar ve zaman
    sessizce 3 saat kayar — hata vermeden, yanlış. Bu test onu engeller.
    """
    print("\nPANEL ZAMAN GÖSTERİMİ")
    from src import panel
    from tests.js_tarayici import _script_cikar

    state, _, _, _ = kur()
    poz(state)
    piyasa = MagicMock()
    piyasa.last_price.return_value = 112.0
    with patch.object(panel, "state", state), \
         patch.object(panel, "market", piyasa), \
         patch.object(panel, "build_watchlist", lambda _: []):
        d = panel.build_state()

    an = datetime.fromisoformat(d["now"])
    assert an.tzinfo is not None, "'now' ofsetsiz gidiyor — tarayıcı yerel sanar"
    assert abs((datetime.now(timezone.utc) - an).total_seconds()) < 120
    ok("'now' ofsetli ISO olarak gidiyor")

    (p,) = d["positions"]
    giris = datetime.fromisoformat(p["since"])
    assert giris.tzinfo is not None, "'since' kırpılmış — 3 saatlik sessiz kayma riski"
    ok("'since' TAM ISO (ofset korunmuş, kırpılmamış)")

    # JS tarafı: sabit "UTC" etiketi ve zaman damgası kırpması kalmamalı.
    js = _script_cikar(panel.PAGE)
    assert " UTC</td>" not in js, "sabit UTC etiketi geri gelmiş"
    assert "slice(11,16)" not in js, "zaman damgası yine kırpılıyor"
    assert "toLocaleTimeString" in js and "_dt" in js
    ok("JS sabit UTC etiketi kullanmıyor, yerel saate çeviriyor")


def test_deploy_teshis():
    """Dağıtım teşhis ucu: çökmeden durum döndürmeli, tetik korunmalı.

    Otomatik güncelleme 2026-09-09'da sessizce çalışmadı ve sebebi bulunamadı,
    çünkü sunucudaki log yalnızca ssh ile okunabiliyordu — teşhis edilemeyen
    güvenlik kapısı, kapı değil kör noktadır. Bu uç log'u HTTP'ye açar.

    Teşhis fonksiyonu ASLA patlamamalı: patlarsa arıza anında (tam da lazım
    olduğu anda) elimizde hiçbir şey kalmaz.
    """
    print("\nDAĞITIM TEŞHİSİ")
    from src import panel

    d = panel.deploy_durum()
    for alan in ("yerel_surum", "uzak_dal", "crontab", "betik_var",
                 "son_cron_kosusu", "kilit_serbest",
                 "panel_erisilemedi_damgasi", "su_an_calisiyor", "log"):
        assert alan in d, f"teşhis alanı eksik: {alan}"
    ok("deploy_durum() beklenen alanların hepsini döndürdü")

    # Alt komutlar patlasa bile teşhis ayakta kalmalı.
    with patch.object(panel.subprocess, "run", side_effect=OSError("git yok")):
        d2 = panel.deploy_durum()
    assert d2["yerel_surum"]["kod"] == -1 and "git yok" in d2["yerel_surum"]["cikti"]
    ok("alt komut patlasa da teşhis çökmüyor, hatayı raporluyor")

    # Log okunamasa bile çökmemeli.
    with patch.object(panel, "GUNCELLE_LOG", Path("/olmayan/dizin/x.log")):
        assert isinstance(panel.deploy_durum()["log"], list)
    ok("log dosyası yoksa boş liste (çökme yok)")

    # Tetik: betik yoksa reddetmeli, iş parçacığı BAŞLATMAMALI.
    with patch.object(panel, "GUNCELLE_BETIK", Path("/olmayan/betik.sh")), \
         patch.object(panel.threading, "Thread") as sahte:
        r = panel.guncellemeyi_tetikle()
    assert r["ok"] is False and not sahte.called
    ok("betik yoksa tetik reddediliyor (boşuna süreç doğmuyor)")

    # Zaten sürüyorsa ikinci tetik reddedilmeli (eşzamanlı iki dağıtım olmaz).
    panel._guncelle_calisiyor.set()
    try:
        with patch.object(panel.threading, "Thread") as sahte:
            r = panel.guncellemeyi_tetikle()
        assert r["ok"] is False and "sürüyor" in r["hata"] and not sahte.called
    finally:
        panel._guncelle_calisiyor.clear()
    ok("güncelleme sürerken ikinci tetik reddediliyor")

    # Tetik SABİT betiği çağırmalı — dışarıdan parametre almadığını sabitler.
    with patch.object(panel.threading, "Thread") as sahte:
        r = panel.guncellemeyi_tetikle()
    assert r["ok"] is True and sahte.called
    ok("geçerli durumda tetik arka planda başlatılıyor")

    # Kalp atışı: sessizlik "yolunda" mı "ölü" mü — ayırt edilebilmeli.
    kok = Path(tempfile.mkdtemp())
    (kok / "logs").mkdir()
    with patch.object(panel, "PROJECT_ROOT", kok):
        h = panel._son_cron_kosusu()
        assert h["saglikli"] is False and "hiç koşmamış" in h.get("not", "")
        ok("damga yoksa 'sağlıklı değil' (cron ölü, sessizlik yutulmuyor)")

        taze = datetime.now(timezone.utc).isoformat()
        (kok / "logs" / ".son-kosu").write_text(taze, encoding="utf-8")
        assert panel._son_cron_kosusu()["saglikli"] is True
        ok("taze damga → sağlıklı")

        bayat = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        (kok / "logs" / ".son-kosu").write_text(bayat, encoding="utf-8")
        h = panel._son_cron_kosusu()
        assert h["saglikli"] is False and h["yas_sn"] > 900
        ok("20 dk önceki damga → 3 koşu kaçmış, arıza olarak işaretlendi")

        (kok / "logs" / ".son-kosu").write_text("bozuk", encoding="utf-8")
        assert panel._son_cron_kosusu()["saglikli"] is False
        ok("bozuk damga → çökmüyor, sağlıksız sayıyor")


def test_dagitim_gerilik_alarmi():
    """Dağıtım döngüsü öldüğünde bunu PANEL bağırmalı — ölen kendini duyuramaz.

    2026-09-15 ARIZASI: sızmış bir dosya tanıtıcısı `flock`u tutuyordu; güncelleme
    betiği BEŞ GÜN boyunca her cron turunda kalp atışını yazıp sessizce çıktı.
    Tek fetch bile yapmadığı için `origin/main` de bayat kaldı ve üç gösterge
    birden yeşil göründü: kalp atışı taze, `git status` "geride değilsin",
    panel "cron: sağlıklı". Bu dosyanın değişmez dersi: başarısızlık başarıyla
    aynı görünmemeli.

    Buradaki testler iki şeyi çivilyor:
      1) "cron tetiklendi" ile "kontrol tamamlandı" ayrı ayrı ölçülüyor,
      2) gerilik sayacı panel yeniden başlayınca SIFIRLANMIYOR (nöbetçi paneli
         5 dakikada bir diriltebiliyor; bellekte tutulsa eşiğe hiç varılmazdı).
    """
    print("\nDAĞITIM GERİLİK ALARMI")
    from src import panel

    TAZE = {"zaman": "x", "yas_sn": 60, "saglikli": True, "sonuc": "guncel"}
    AYNI = "a" * 40

    # --- 1) Her şey yolunda: alarm YOK (yanlış alarm da güveni öldürür)
    d = panel._dagitim_degerlendir(AYNI, AYNI, None, TAZE, 99999)
    assert d["alarm"] is False and d["geride"] is False, d
    ok("yerel = uzak ve kontrol taze → alarm yok")

    # --- 2) Geride ama HENÜZ kısa süredir: alarm yok (tek tur kaçmak gürültü)
    d = panel._dagitim_degerlendir("a" * 40, "b" * 40, 300, TAZE, 99999)
    assert d["geride"] is True and d["alarm"] is False, d
    ok("5 dakikadır geride → henüz alarm yok (eşik 15 dk)")

    # --- 3) Eşiği aştı: alarm ve sebepte İKİ sha da görünmeli
    d = panel._dagitim_degerlendir("a" * 40, "b" * 40, 1200, TAZE, 99999)
    assert d["alarm"] is True and "aaaaaaa" in d["sebep"] and "bbbbbbb" in d["sebep"]
    assert "20 dakika" in d["sebep"], d["sebep"]
    ok("20 dakikadır geride → alarm, sebepte süre ve iki sürüm birden")

    # --- 4) ASIL ARIZA: yeni commit YOK ama betik kontrolü bitiremiyor.
    # Gerilik yok, yine de alarm çalmalı — 2026-09-15'te tam olarak bu oldu.
    bayat = {"zaman": "x", "yas_sn": 5 * 86400, "saglikli": False}
    d = panel._dagitim_degerlendir(AYNI, AYNI, None, bayat, 99999)
    assert d["geride"] is False and d["alarm"] is True, d
    assert "bitiremiyor" in d["sebep"], d["sebep"]
    ok("sha'lar eşit ama kontrol 5 gündür bitmiyor → alarm [2026-09-15 regresyonu]")

    # --- 5) Damga yoksa: yeni kurulumda SUSMALI, uzun süredir yoksa BAĞIRMALI
    yok = {"zaman": None, "yas_sn": None, "saglikli": False}
    assert panel._dagitim_degerlendir(AYNI, AYNI, None, yok, 60)["alarm"] is False
    ok("damga yok + gözcü yeni başladı → alarm yok (taze kurulum yanlış alarm vermez)")
    d = panel._dagitim_degerlendir(AYNI, AYNI, None, yok, 1200)
    assert d["alarm"] is True and "hiç tamamlanmış kontrol" in d["sebep"]
    ok("damga yok + 20 dakika geçti → alarm (hiç yazılmıyor demektir)")

    kok = Path(tempfile.mkdtemp())
    (kok / "logs").mkdir()
    with patch.object(panel, "SON_KONTROL", kok / "logs" / ".son-kontrol"), \
         patch.object(panel, "GERILIK_BASI", kok / "logs" / ".gerilik-basladi"), \
         patch.object(panel, "GERILIK_ALARM_DAMGASI", kok / "logs" / ".gerilik-bildirildi"):

        # --- 6) Damga okuma: betiğin yazdığı biçim birebir ayrışmalı
        assert panel._son_kontrol()["saglikli"] is False
        ok("kontrol damgası yoksa 'sağlıklı' denmiyor")

        zaman = datetime.now(timezone.utc).isoformat(timespec="seconds")
        panel.SON_KONTROL.write_text(f"{zaman} {AYNI} {AYNI} guncel\n", encoding="utf-8")
        s = panel._son_kontrol()
        assert s["saglikli"] is True and s["sonuc"] == "guncel" and s["yerel"] == AYNI
        ok("betiğin yazdığı damga ayrışıyor: zaman + iki sha + sonuç etiketi")

        eski = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        panel.SON_KONTROL.write_text(f"{eski} {AYNI} {AYNI} guncel\n", encoding="utf-8")
        assert panel._son_kontrol()["saglikli"] is False
        ok("3 saatlik damga → sağlıksız (kalp atışı taze olsa bile)")

        panel.SON_KONTROL.write_text("bozuk içerik", encoding="utf-8")
        assert panel._son_kontrol()["saglikli"] is False
        ok("bozuk damga → çökmüyor, sağlıksız sayıyor")

        # --- 7) GERİLİK SAYACI DİSKTE: panel restart'ı sayacı sıfırlamamalı
        t0 = datetime.now(timezone.utc)
        assert panel._gerilik_suresi(True, t0) == 0
        assert panel.GERILIK_BASI.exists(), "gerilik başlangıcı diske yazılmadı"
        # 20 dakika sonra panel yeniden başlamış gibi (bellek yok, sadece dosya)
        assert panel._gerilik_suresi(True, t0 + timedelta(minutes=20)) == 1200
        ok("gerilik sayacı diskte tutuluyor → panel restart'ı eşiği sıfırlamıyor")

        # Gerilik bitince iz KALMAMALI, yoksa alarm bir daha hiç susmaz.
        panel.GERILIK_ALARM_DAMGASI.write_text("x", encoding="utf-8")
        assert panel._gerilik_suresi(False, t0) is None
        assert not panel.GERILIK_BASI.exists() and not panel.GERILIK_ALARM_DAMGASI.exists()
        ok("gerilik kapanınca sayaç ve alarm damgası siliniyor")

        # --- 8) Telegram: alarm BİR KEZ gider (her 5 dk tekrar = gürültü = görmezden gelme)
        with patch("src.notifier.TelegramNotifier") as sahte:
            panel._gerilik_bildir({"alarm": True, "sebep": "test"})
            panel._gerilik_bildir({"alarm": True, "sebep": "test"})
            assert sahte.return_value.send_error.call_count == 1, "alarm tekrar tekrar gitti"
            mesaj = sahte.return_value.send_error.call_args[0][0]
            assert "ESKİ sürümle" in mesaj and "deploy-durum" in mesaj
        ok("gerilik alarmı Telegram'a BİR KEZ gidiyor, ne yapılacağını da yazıyor")

        # Telegram patlasa bile gözcü ölmemeli — panel bandı ikinci kanal.
        with patch("src.notifier.TelegramNotifier", side_effect=OSError("ağ yok")):
            panel.GERILIK_ALARM_DAMGASI.unlink(missing_ok=True)
            panel._gerilik_bildir({"alarm": True, "sebep": "test"})
        ok("Telegram bozuksa alarm yutuluyor ama gözcü çökmüyor (bant hâlâ kırmızı)")

    # --- 9) Uçlar ve panel bandı: veri yayınlanmazsa alarm görünmez
    d = panel.deploy_durum()
    assert "son_kontrol" in d and "dagitim" in d, "teşhis ucunda yeni alanlar yok"
    ok("/api/deploy-durum hem kontrol damgasını hem gerilik durumunu yayınlıyor")
    assert "function dagitim" in panel.PAGE or 'id="dagUyari"' in panel.PAGE
    assert "d.dagitim" in panel.PAGE, "panel gerilik durumunu okumuyor"
    assert "DÖNGÜSÜ arızalı" in panel.PAGE, "kırmızı bant metni yok"
    ok("panelde kırmızı dağıtım bandı var ve /api/state'ten besleniyor")


# ------------------------------------------------------------- BORSA kanalı
def borsa_kur(sembol="NVDA"):
    """İzole borsa trader'ı — ağ YOK, market sahte."""
    from src.borsa_trader import BorsaPaperTrader
    db = Path(tempfile.mkdtemp()) / "borsa_test.db"
    state = StateStore(db)
    market = MagicMock()
    market.last_price.return_value = 100.0
    market.klines.return_value = None       # mum yok → sinyal akışı çalışmaz
    market.haftalik_yukari.return_value = True
    notifier = MagicMock()
    cfg = replace(CONFIG, borsa_max_positions=3, borsa_risk_pct=0.01)
    return state, notifier, market, BorsaPaperTrader(sembol, cfg, market,
                                                     state, notifier)


def borsa_poz(state, sym="NVDA", entry=100.0, stop=90.0, side="LONG",
              qty=10.0, margin=1000.0):
    p = Position(symbol=sym, qty=qty, entry_price=entry, highest_price=entry,
                 trailing_stop=stop, stop_order_id=None,
                 entry_time=datetime.now(timezone.utc).isoformat(),
                 entry_fee_usdt=0.5, side=side, margin=margin, funding_acc=0.0,
                 risk_unit=10.0)
    state.save_position(p)
    return p


def test_borsa_kanali():
    print("\nBORSA KANALI (ABD + BIST, sanal cüzdan)")
    from src.borsa_data import para_birimi
    from src.borsa_trader import BASLANGIC, SLIPPAGE as BSLIP

    # Para birimi eşlemesi — cüzdan seçiminin temeli, yanılırsa muhasebe karışır.
    assert para_birimi("NVDA") == "USD" and para_birimi("THYAO.IS") == "TRY"
    assert para_birimi("thyao.is") == "TRY", "küçük harf sembol yanlış cüzdana gitti"
    ok("para birimi eşlemesi: .IS → TRY, diğerleri → USD")

    # Cüzdanlar bağımsız başlar.
    state, _, _, t_abd = borsa_kur("NVDA")
    _, _, _, t_bist = borsa_kur("THYAO.IS")
    assert t_abd._balance() == BASLANGIC["USD"]
    assert t_bist._balance() == BASLANGIC["TRY"]
    ok("USD ve TRY cüzdanları kendi başlangıç değerleriyle açılıyor")

    # GAP DÜRÜSTLÜĞÜ: fiyat stopun ÜZERİNDEN atladıysa çıkış stop'tan değil
    # GERÇEKLEŞEN fiyattan olmalı. (Kriptoda stop fiyatı varsayımı makul,
    # hissede gece gap'i bunu yalanlar — bu kanalın var olma sebeplerinden.)
    state, _, market, t = borsa_kur("NVDA")
    p = borsa_poz(state, "NVDA", entry=100.0, stop=90.0)
    cikis = t._stop_exit_price(p, 80.0)     # fiyat 90 stopunun altına GAP'lemiş
    assert cikis is not None and abs(cikis - 80.0 * (1 - BSLIP)) < 1e-9, \
        f"gap'te stop fiyatından çıkılmış: {cikis}"
    ok("LONG gap: stop 90 ama fiyat 80 → çıkış 80'den (stop fiyatı hayal)")

    cikis = t._stop_exit_price(p, 89.0)     # normal tetiklenme, gap yok
    assert cikis is not None and abs(cikis - 89.0 * (1 - BSLIP)) < 1e-9
    assert t._stop_exit_price(p, 95.0) is None, "stop üstünde fiyatta tetiklendi"
    ok("stop normal tetiklenme + tetiklenmeme sınırları doğru")

    sp = borsa_poz(state, "TSLA", entry=100.0, stop=110.0, side="SHORT")
    cikis = t._stop_exit_price(sp, 120.0)   # short'ta yukarı gap
    assert cikis is not None and abs(cikis - 120.0 * (1 + BSLIP)) < 1e-9
    ok("SHORT gap: stop 110 ama fiyat 120 → çıkış 120'den")

    # BIST'TE SHORT YASAK — canlıda yapamayacağımızı kağıtta ölçmek yalan olur.
    state, _, market, t = borsa_kur("THYAO.IS")
    satir = pd.Series({"close": 80.0, "ema_trend": 100.0, "rsi": 40.0,
                       "atr": 2.0, "donchian_high": 120.0, "donchian_low": 85.0})
    market.haftalik_yukari.return_value = False   # short'a uygun ortam bile olsa
    with patch.object(t, "_open") as acilis:
        t._try_enter(satir, MagicMock(decision="SHORT-UYGUN", score=-70))
    assert not acilis.called, "BIST sembolünde SHORT açıldı!"
    ok("BIST'te short kırılımı gelse de pozisyon AÇILMIYOR (long-only)")

    # Aynı satır ABD sembolünde short açabilmeli (kural piyasaya özgü, genel değil).
    state, _, market, t = borsa_kur("TSLA")
    with patch.object(t, "_open") as acilis:
        t._try_enter(satir, MagicMock(decision="SHORT-UYGUN", score=-70))
    assert acilis.called, "ABD sembolünde geçerli short reddedildi"
    ok("aynı sinyal ABD sembolünde short açıyor (kısıt yalnız BIST'te)")

    # Cüzdan muhasebesi: açılışta nakit düşer, kapanışta tutar+kâr döner.
    state, notifier, market, t = borsa_kur("NVDA")
    market.last_price.return_value = 100.0
    t._open("LONG", atr=2.0, reason="test")
    p = state.get_position("NVDA")
    assert p is not None and p.qty >= 1 and p.qty == int(p.qty), "tam hisse değil"
    assert p.trailing_stop < p.entry_price, "stop girişin üstünde"
    assert t._balance() < BASLANGIC["USD"], "açılış nakit düşürmedi"
    ara = t._balance()
    t._close(p, 110.0, "test kapanışı")
    assert t._balance() > ara, "kapanış parayı geri koymadı"
    assert state.get_position("NVDA") is None
    islemler = state.recent_trades(5)
    assert len(islemler) == 1 and islemler[0]["pnl_usdt"] > 0
    ok("cüzdan muhasebesi: aç → nakit düşer, kârla kapat → nakit artar")

    # KOMUT: pozisyon yokken CLOSE → red izi; varken CLOSE → kapanır + ok izi.
    state, _, market, t = borsa_kur("NVDA")
    state.set_kv("bcmd_NVDA", "CLOSE")
    t._process_manual_commands(None, 100.0)
    assert state.get_kv("bcmdres_NVDA", "").startswith("red|")
    ok("pozisyonsuz CLOSE → 'red' izi (sessiz yutma yok)")

    p = borsa_poz(state, "NVDA")
    state.set_kv("bcmd_NVDA", "CLOSE")
    sonuc = t._process_manual_commands(p, 105.0)
    assert sonuc is None and state.get_position("NVDA") is None
    assert state.get_kv("bcmdres_NVDA", "").startswith("ok|")
    ok("pozisyonlu CLOSE → kapandı + 'ok' izi")

    # Fiyat YOKKEN kapatma reddedilir — bilinmeyen fiyattan işlem olmaz.
    p = borsa_poz(state, "NVDA")
    state.set_kv("bcmd_NVDA", "CLOSE")
    sonuc = t._process_manual_commands(p, None)
    assert sonuc is not None and state.get_position("NVDA") is not None
    assert state.get_kv("bcmdres_NVDA", "").startswith("red|")
    ok("fiyat alınamıyorken CLOSE → red + pozisyon duruyor (körlemesine işlem yok)")

    # Günlük kesici: cüzdan gün başından %5+ eridiyse yeni giriş yok.
    state, _, market, t = borsa_kur("NVDA")
    bugun = datetime.now(timezone.utc).date().isoformat()
    state.set_kv("borsa_gunbasi_USD", f"{bugun}|20000.0")   # gün başı 20k, şimdi 10k
    assert t._kesici_devrede() is True
    with patch.object(t, "_open") as acilis:
        uygun = pd.Series({"close": 130.0, "ema_trend": 100.0, "rsi": 60.0,
                           "atr": 2.0, "donchian_high": 125.0, "donchian_low": 85.0})
        t._try_enter(uygun, MagicMock(decision="LONG-UYGUN", score=70))
    assert not acilis.called, "kesici devredeyken giriş açıldı"
    ok("günlük kesici devredeyken yeni giriş engelleniyor")

    # MANUEL STOP — kriptodaki kuralların aynası: anında tetikleyen RED,
    # sıkma serbest, gevşetme kilit kurar, iz sürme kilit açılana dek durur.
    state, _, market, t = borsa_kur("NVDA")
    state.set_kv("bcmd_NVDA", "STOP:95")
    t._process_manual_commands(None, 100.0)
    assert state.get_kv("bcmdres_NVDA", "").startswith("red|")
    ok("pozisyon yokken STOP → 'red' izi")

    p = borsa_poz(state, "NVDA", entry=100.0, stop=90.0)
    state.set_kv("bcmd_NVDA", "STOP:105")            # LONG'da fiyat üstü stop
    p = t._process_manual_commands(p, 100.0)
    assert p.trailing_stop == 90.0, "anında tetikleyen stop kabul edildi!"
    assert "kapatırdı" in state.get_kv("bcmdres_NVDA", "")
    ok("anında tetikleyecek stop reddedildi (KAPAT'a yönlendirme)")

    state.set_kv("bcmd_NVDA", "STOP:95")             # sıkma: 90 → 95
    p = t._process_manual_commands(p, 100.0)
    assert p.trailing_stop == 95.0 and p.stop_manual_ref == 0.0
    assert state.get_kv("bcmdres_NVDA", "").startswith("ok|")
    ok("stop sıkma uygulanıyor, kilit kurulmuyor (cırcır zaten korur)")

    state.set_kv("bcmd_NVDA", "STOP:85")             # gevşetme: 95 → 85
    p = t._process_manual_commands(p, 100.0)
    assert p.trailing_stop == 85.0 and abs(p.stop_manual_ref - 105.0) < 1e-9, \
        f"kilit ref yanlış: {p.stop_manual_ref} (2×95−85=105 olmalı)"
    ok("gevşetme: ref = 2×eski − yeni kilidi kuruldu")

    # Kilit varken iz sürme DURMALI (aday ref'in altında kaldıkça).
    t._update_trailing(p, mum(high=101.0, low=95.0, atr=1.0))   # aday 101−3=98 < 105
    p = state.get_position("NVDA")
    assert p.trailing_stop == 85.0 and p.stop_manual_ref > 0, "kilit ihlal edildi"
    ok("kilit açıkken iz süren stop gevşetilmiş seviyeye dokunmuyor")

    # İşlem payı geri kazanınca (aday > ref) kilit açılır, iz sürme döner.
    t._update_trailing(p, mum(high=110.0, low=100.0, atr=1.0))  # aday 110−3=107 > 105
    p = state.get_position("NVDA")
    assert p.stop_manual_ref == 0.0 and p.trailing_stop == 107.0
    ok("aday ref'i geçince kilit kendiliğinden açıldı, stop yükseldi")

    # Fiyat alınamıyorken STOP komutu ertelenir (körlemesine değişiklik yok).
    state.set_kv("bcmd_NVDA", "STOP:100")
    p = t._process_manual_commands(p, None)
    assert p.trailing_stop == 107.0
    assert state.get_kv("bcmdres_NVDA", "").startswith("red|")
    ok("fiyatsızken STOP → red + seviye değişmedi")

    # R BİLDİRİMİ — bildirir ama KAPATMAZ, aynı eşiği tekrar bildirmez.
    state, notifier, market, t = borsa_kur("NVDA")
    p = borsa_poz(state, "NVDA", entry=100.0, stop=97.0)
    p.risk_unit = 3.0                                # 1R = 3
    state.save_position(p)
    notifier.reset_mock()
    t._check_r_notify(p, 112.0)                      # (112−100)/3 = 4R
    assert notifier.send.called, "4R'de bildirim gitmedi"
    assert state.get_position("NVDA") is not None, "R bildirimi pozisyonu KAPATTI!"
    assert state.get_position("NVDA").r_notified == 4.0
    ok("4R'de bildirim gitti, pozisyon AÇIK kaldı")

    notifier.reset_mock()
    t._check_r_notify(state.get_position("NVDA"), 113.0)   # hâlâ 4R bandında
    assert not notifier.send.called, "aynı eşik ikinci kez bildirildi (spam)"
    ok("aynı R eşiği ikinci kez bildirilmiyor")

    # Panel: /api/borsa veri sözleşmesi — sekmenin beklediği alanlar eksiksiz.
    import src.panel as panel
    borsa_poz(state, "NVDA", entry=100.0, stop=95.0)   # risk_unit=10 (borsa_poz)
    with patch.object(panel, "borsa_state", state):
        d = panel.build_borsa_state()
    for alan in ("enabled", "canli", "cuzdanlar", "positions", "komutlar",
                 "assessments", "trades", "symbols"):
        assert alan in d, f"/api/borsa alanı eksik: {alan}"
    assert {c["cur"] for c in d["cuzdanlar"]} == {"USD", "TRY"}
    pp = next(x for x in d["positions"] if x["symbol"] == "NVDA")
    for alan in ("r_simdi", "r_hedef", "stop_pnl", "stop_kilit"):
        assert alan in pp, f"pozisyonda R/stop alanı eksik: {alan}"
    assert abs(pp["stop_pnl"] - 10 * (95.0 - 100.0)) < 1e-9, "stop_pnl hesabı yanlış"
    ok("/api/borsa sözleşmesi tam (iki cüzdan + R + stop alanları)")

    # Panel JS: borsa sekmesinin id'leri gerçekten sayfada (test 47 dinamikti,
    # burada sekmeye ÖZGÜ olanları sabitliyoruz ki kablo kopukluğu yakalansın).
    from src.panel import PAGE
    for eid in ("sayfaBorsa", "tabBorsa", "bStats", "bPositions",
                "bKomutlar", "bAssess", "bTrades"):
        assert f'id="{eid}"' in PAGE, f"borsa sekmesi id eksik: {eid}"
    ok("borsa sekmesinin tüm id'leri sayfada tanımlı")


def test_telegram_saglik():
    """2026-09-10: bot OPUSDT SHORT açtı, token yenilenmişti, HİÇ bildirim
    gitmedi ve hiçbir yerde iz kalmadı. Bu testler o sessizliği yasaklar."""
    print("\nBİLDİRİM KANALI (kendi arızasını kendi kanalından duyuramaz)")
    from src.notifier import SAGLIK_ANAHTARI, TelegramNotifier

    state, _, _, _ = kur()

    # 1) Token EKSİK → sessizce devre dışı kalmak YASAK, iz bırakmalı.
    n = TelegramNotifier("", "12345", saglik_yaz=state.set_kv)
    kayit = json.loads(state.get_kv(SAGLIK_ANAHTARI))
    assert kayit["ok"] is False and "TELEGRAM_BOT_TOKEN" in kayit["sebep"]
    assert n.send("deneme") is False, "kapalıyken send True döndü"
    ok("token eksikse durum 'arızalı' yazılıyor (eskiden tamamen sessizdi)")

    n2 = TelegramNotifier("abc", "", saglik_yaz=state.set_kv)
    assert "TELEGRAM_CHAT_ID" in json.loads(state.get_kv(SAGLIK_ANAHTARI))["sebep"]
    ok("chat_id eksikse de sebep adıyla raporlanıyor")

    # 2) Token GEÇERSİZ (401) → sebep gövdeden okunup kaydedilmeli.
    state2, _, _, _ = kur()
    n3 = TelegramNotifier("eski_token", "42", saglik_yaz=state2.set_kv)
    sahte = MagicMock(status_code=401)
    sahte.json.return_value = {"description": "Unauthorized"}
    with patch("src.notifier.requests.post", return_value=sahte):
        assert n3.send("deneme") is False
    kayit = json.loads(state2.get_kv(SAGLIK_ANAHTARI))
    assert kayit["ok"] is False and "Unauthorized" in kayit["sebep"]
    ok("401'de sebep ('Unauthorized') kaydediliyor, sessizce yutulmuyor")

    # 3) Başarılı gönderim durumu TEMİZLEMELİ (arıza asılı kalmasın).
    with patch("src.notifier.requests.post", return_value=MagicMock(status_code=200)):
        assert n3.send("deneme") is True
    assert json.loads(state2.get_kv(SAGLIK_ANAHTARI))["ok"] is True
    ok("gönderim düzelince durum 'sağlıklı'ya dönüyor")

    # 4) Açılış doğrulaması: ilk işlemi BEKLEMEDEN token'ı sınar.
    state3, _, _, _ = kur()
    n4 = TelegramNotifier("token", "42", saglik_yaz=state3.set_kv)
    with patch("src.notifier.requests.get", return_value=MagicMock(status_code=401)):
        assert n4.dogrula() is False
    assert "401" in json.loads(state3.get_kv(SAGLIK_ANAHTARI))["sebep"]
    ok("açılışta getMe ile sınama — bozuk token işlem beklemeden yakalanıyor")

    # 5) Panel: durum /api/state'e çıkmalı ve "kayıt yok" ≠ "sorun yok".
    import src.panel as panel
    bos, _, _, _ = kur()
    with patch.object(panel, "state", bos):
        assert panel.telegram_saglik()["ok"] is None, "kayıt yokken 'sağlıklı' sanıldı"
    with patch.object(panel, "state", state2):
        assert panel.telegram_saglik()["ok"] is True
    ok("panel: kayıt yoksa 'bilinmiyor' (sessizlik 'yolunda' sayılmıyor)")

    assert 'id="tgUyari"' in panel.PAGE and "ÇALIŞMIYOR" in panel.PAGE
    ok("panelde arıza bandının kabı ve metni mevcut")

    # 6) Bot günlüğü teşhis ucu — ssh kapalıyken kör kalmayalım.
    kok = Path(tempfile.mkdtemp())
    kayit = kok / "bot.log"
    kayit.write_text("\n".join(f"satir {i} OPUSDT" if i % 2 else f"satir {i} baska"
                               for i in range(300)), encoding="utf-8")
    # Config donmuş bir dataclass — alanı doğrudan yamalanamaz, kopyası konur.
    with patch.object(panel, "CONFIG", replace(CONFIG, log_path=kayit)):
        r = panel.bot_log(10)
        assert r["var"] is True and len(r["satirlar"]) == 10
        s = panel.bot_log(500, "OPUSDT")
        assert all("OPUSDT" in x for x in s["satirlar"]), "filtre sızdırdı"
        assert len(panel.bot_log(9999)["satirlar"]) <= 500, "üst sınır aşıldı"
    with patch.object(panel, "CONFIG", replace(CONFIG, log_path=kok / "yok.log")):
        assert panel.bot_log()["var"] is False   # dosya yoksa çökmemeli
    ok("bot günlüğü ucu: kuyruk + filtre + üst sınır + dosyasızlık")


def sahte_broker(cfg=None):
    """Gerçek Binance'e DOKUNMAYAN FuturesBroker. binance.client.Client
    içeri alınmadan yamalanır; ağ isteği hiç doğmaz."""
    from src import futures_exchange as fx
    istemci = MagicMock()
    istemci.futures_exchange_info.return_value = {"symbols": [{
        "symbol": "BTCUSDT",
        "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"}]}]}
    sahte_modul = MagicMock()
    sahte_modul.Client.return_value = istemci
    with patch.dict(sys.modules, {"binance": MagicMock(),
                                  "binance.client": sahte_modul}):
        b = fx.FuturesBroker(cfg or replace(CONFIG, mode="futures_testnet"))
    return b, istemci


def test_canli_altyapi():
    """CANLIYA GEÇİŞ KAPISI. Buradaki her test, gerçek parayla yapılması
    felaket olacak bir hatayı yasaklar."""
    print("\nCANLI ALTYAPI (gerçek para yolu)")
    from src import futures_exchange as fx

    # --- Fiyat/miktar yuvarlama: yukarı yuvarlamak emir reddi üretir ---
    assert fx.asagi_yuvarla(1.23987, 0.001) <= 1.23987
    assert abs(fx.asagi_yuvarla(0.7, 0.1) - 0.7) < 1e-9, "kayan nokta hatası"
    assert abs(fx.fiyat_yuvarla(123.456, 0.10) - 123.5) < 1e-9
    ok("miktar AŞAĞI, fiyat tick'e yuvarlanıyor (emir reddi önleniyor)")

    # --- STOPSUZ POZİSYON YASAK: stop kurulamazsa pozisyon geri kapatılır ---
    b, c = sahte_broker()
    c.futures_create_order.side_effect = [
        {"executedQty": "0.5", "avgPrice": "100", "orderId": 1},   # giriş dolar
        Exception("stop reddedildi"),                              # STOP başarısız
        {"executedQty": "0.5", "avgPrice": "99.9", "orderId": 3},  # geri kapatma
    ]
    sonuc = b.giris_ve_stop("BTCUSDT", "LONG", 0.5, 95.0)
    assert sonuc is None, "stop kurulamadı ama pozisyon açık bırakıldı!"
    assert c.futures_create_order.call_count == 3, "geri kapatma emri gönderilmedi"
    kapanis = c.futures_create_order.call_args_list[2].kwargs
    assert kapanis["side"] == "SELL" and kapanis["type"] == "MARKET"
    ok("stop kurulamazsa pozisyon DERHAL kapatılıyor ('stop daima olacak')")

    # --- Kapatma da başarısızsa ACİL bayrağı: sessizce geçilmez ---
    b, c = sahte_broker()
    c.futures_create_order.side_effect = [
        {"executedQty": "0.5", "avgPrice": "100", "orderId": 1},
        Exception("stop reddedildi"),
        Exception("kapatma da reddedildi"),
    ]
    sonuc = b.giris_ve_stop("BTCUSDT", "LONG", 0.5, 95.0)
    assert sonuc is not None and sonuc["acil"] is True and sonuc["stop_id"] is None
    ok("stop YOK + kapatma da başarısız → 'acil' bayrağı (sessiz geçiş yok)")

    # --- Mutlu yol ---
    b, c = sahte_broker()
    c.futures_create_order.side_effect = [
        {"executedQty": "0.5", "avgPrice": "100", "orderId": 1},
        {"orderId": 77},
    ]
    sonuc = b.giris_ve_stop("BTCUSDT", "LONG", 0.5, 95.0)
    assert sonuc["acil"] is False and sonuc["stop_id"] == 77 and sonuc["giris"] == 100.0
    stop_cagri = c.futures_create_order.call_args_list[1].kwargs
    assert stop_cagri["type"] == "STOP_MARKET" and stop_cagri["closePosition"] is True
    assert stop_cagri["side"] == "SELL", "LONG'un stopu SELL olmalı"
    ok("giriş + borsa taraflı STOP_MARKET (closePosition) birlikte kuruluyor")

    # --- İz süren stop: ÖNCE yeni kurulur, SONRA eski iptal edilir ---
    b, c = sahte_broker()
    c.futures_create_order.return_value = {"orderId": 99}
    yeni = b.stop_tasi("BTCUSDT", "LONG", eski_id=55, yeni_stop=97.0)
    assert yeni == 99 and c.futures_cancel_order.called
    ok("stop taşımada sıra doğru: yeni kurulmadan eski iptal edilmiyor")

    # Yeni stop kurulamazsa ESKİSİ KORUNUR (iptal edilmez!) — aksi hâlde
    # pozisyon tamamen stopsuz kalırdı.
    b, c = sahte_broker()
    c.futures_create_order.side_effect = Exception("red")
    yeni = b.stop_tasi("BTCUSDT", "LONG", eski_id=55, yeni_stop=97.0)
    assert yeni == 55 and not c.futures_cancel_order.called, "eski stop iptal edildi!"
    ok("yeni stop kurulamazsa eski stop KORUNUYOR (korumasız an oluşmuyor)")

    # --- SHORT tarafı: stop BUY olmalı ---
    b, c = sahte_broker()
    c.futures_create_order.return_value = {"orderId": 5}
    b.stop_kur("BTCUSDT", "SHORT", 105.0)
    assert c.futures_create_order.call_args.kwargs["side"] == "BUY"
    ok("SHORT pozisyonun stopu BUY yönünde kuruluyor")

    # --- Borsadaki gerçek pozisyon okuması (mutabakatın temeli) ---
    b, c = sahte_broker()
    c.futures_position_information.return_value = [
        {"positionAmt": "-0.30", "entryPrice": "100", "leverage": "2",
         "liquidationPrice": "150"}]
    p = b.pozisyon("BTCUSDT")
    assert p["yon"] == "SHORT" and p["miktar"] == 0.30
    c.futures_position_information.return_value = [{"positionAmt": "0"}]
    assert b.pozisyon("BTCUSDT") is None
    ok("borsadaki gerçek pozisyon doğru okunuyor (yön + miktar)")

    # --- Hedge modu tespiti: 'tek pozisyon' varsayımımızı çürütür ---
    b, c = sahte_broker()
    c.futures_get_position_mode.return_value = {"dualSidePosition": True}
    assert b.tek_yon_modu_mu() is False
    c.futures_get_position_mode.return_value = {"dualSidePosition": False}
    assert b.tek_yon_modu_mu() is True
    ok("hedge modu tespit ediliyor (tek yön varsayımı sınanıyor)")


def canli_kur(sembol="BTCUSDT"):
    """FuturesLiveTrader için: gerçek FuturesBroker AMA istemcisi (Client)
    yamalı, hiçbir ağ isteği doğmaz — sahte_broker() ile aynı desen."""
    db = Path(tempfile.mkdtemp()) / "test.db"
    state = StateStore(db)
    market = MagicMock()
    market.last_price.return_value = 100.0
    breaker = MagicMock()
    breaker.entries_allowed.return_value = (True, 0.0)
    notifier = MagicMock()
    cfg = replace(CONFIG, max_concurrent_positions=4, r_notify_level=4.0,
                  mode="futures_testnet", leverage=2)
    broker, istemci = sahte_broker(replace(CONFIG, mode="futures_testnet"))
    t = FuturesLiveTrader(sembol, cfg, market, state, notifier, breaker, broker)
    return state, notifier, market, broker, t


def test_canli_trader():
    """FuturesLiveTrader — FuturesPaperTrader'ın karar mantığını miras alır,
    yalnızca borsaya dokunan dikişleri gerçek emirlerle değiştirir. Buradaki
    her test paper simülasyonunun canlıda ASLA yapmaması gereken bir şeyi
    (tahmine güvenmek, borsayı sormadan karar vermek) yasaklıyor."""
    print("\nCANLI TRADER (FuturesLiveTrader)")
    from src.futures_exchange import Dolum

    # --- _open mutlu yol: gerçek dolum fiyatı/miktarı ve borsa stop id'si ---
    state, notifier, market, broker, t = canli_kur()
    with patch.object(broker, "bakiye_usdt", return_value=10000.0), \
         patch.object(broker, "varlik_usdt", return_value=10000.0), \
         patch.object(broker, "giris_ve_stop",
                       return_value={"giris": 100.5, "miktar": 0.5, "stop_id": 77, "acil": False}):
        t._open("LONG", atr=3.0, reason="test")
    pos = state.get_position("BTCUSDT")
    assert pos is not None and pos.entry_price == 100.5 and pos.qty == 0.5 and pos.stop_order_id == 77
    ok("canlı _open: gerçek dolum fiyatı/miktarı ve borsa stop id'si kaydediliyor")

    # --- _open: stop kurulamadı → giris_ve_stop None döner → pozisyon YOK ---
    state, notifier, market, broker, t = canli_kur()
    with patch.object(broker, "bakiye_usdt", return_value=10000.0), \
         patch.object(broker, "varlik_usdt", return_value=10000.0), \
         patch.object(broker, "giris_ve_stop", return_value=None):
        t._open("LONG", atr=3.0, reason="test")
    assert state.get_position("BTCUSDT") is None
    ok("canlı _open: stop kurulamayınca pozisyon KAYDEDİLMİYOR (güvenle geri kapatıldı)")

    # --- _open: acil bayrağı → yine de DB'ye yazılır (panelde görünsün) + alarm ---
    state, notifier, market, broker, t = canli_kur()
    with patch.object(broker, "bakiye_usdt", return_value=10000.0), \
         patch.object(broker, "varlik_usdt", return_value=10000.0), \
         patch.object(broker, "giris_ve_stop",
                       return_value={"giris": 100.0, "miktar": 0.5, "stop_id": None, "acil": True}):
        t._open("LONG", atr=3.0, reason="test")
    pos = state.get_position("BTCUSDT")
    assert pos is not None and pos.stop_order_id is None
    assert notifier.send_error.called and "ACİL" in notifier.send_error.call_args[0][0]
    ok("canlı _open: ACİL durumda pozisyon yine DB'ye yazılıyor (görünmez kalmıyor) ve alarm veriliyor")

    # --- stop nöbeti: stop kayıpsa derhal yeniden kuruluyor ---
    state, notifier, market, broker, t = canli_kur()
    p = poz(state, sym="BTCUSDT", entry=100.0, stop=95.0, side="LONG")
    with patch.object(broker, "pozisyon", return_value={"yon": "LONG", "miktar": 10.0}), \
         patch.object(broker, "stop_var_mi", return_value=None), \
         patch.object(broker, "stop_kur", return_value=999) as kur_stop:
        sonuc = t._stop_hit(p, 101.0)
    assert sonuc is False and kur_stop.called
    assert state.get_position("BTCUSDT").stop_order_id == 999
    ok("canlı stop nöbeti: stop kayıpsa derhal yeniden kuruluyor")

    # --- stop nöbeti: yeniden kurulamazsa acil kapatma sinyali (True) ---
    state, notifier, market, broker, t = canli_kur()
    p = poz(state, sym="BTCUSDT", entry=100.0, stop=95.0, side="LONG")
    with patch.object(broker, "pozisyon", return_value={"yon": "LONG", "miktar": 10.0}), \
         patch.object(broker, "stop_var_mi", return_value=None), \
         patch.object(broker, "stop_kur", return_value=None):
        sonuc = t._stop_hit(p, 101.0)
    assert sonuc is True, "stop yeniden kurulamadı ama acil kapatma sinyali verilmedi"
    assert notifier.send_error.called
    ok("canlı stop nöbeti: yeniden kurulamazsa pozisyon acil kapatma sinyali veriyor")

    # --- stop nöbeti: borsada pozisyon zaten yoksa tetiklenmiş sayılır ---
    state, notifier, market, broker, t = canli_kur()
    p = poz(state, sym="BTCUSDT", entry=100.0, stop=95.0, side="LONG")
    with patch.object(broker, "pozisyon", return_value=None):
        assert t._stop_hit(p, 94.0) is True
    ok("canlı stop nöbeti: borsada pozisyon kalmamışsa tetiklenmiş sayılıyor")

    # --- _close: pozisyon hâlâ açıksa GERÇEK emirle kapatılır, tahmin fiyatı GÖRMEZDEN gelinir ---
    state, notifier, market, broker, t = canli_kur()
    p = poz(state, sym="BTCUSDT", entry=100.0, stop=95.0, side="LONG", qty=0.5)
    with patch.object(broker, "pozisyon", return_value={"yon": "LONG", "miktar": 0.5}), \
         patch.object(broker, "pozisyonu_kapat",
                       return_value=Dolum(ort_fiyat=105.0, miktar=0.5, emir_id=5)) as kapat, \
         patch.object(t, "_gercek_net_pnl", return_value=2.4):
        t._close(p, 999.0, "manuel kapatma")   # 999.0 yalnızca bir TAHMİN, kullanılmamalı
    assert kapat.called
    kayit = state.recent_trades(1)[0]
    assert abs(kayit["exit_price"] - 105.0) < 1e-9, "gerçek dolum fiyatı yerine tahmin kullanıldı"
    assert abs(kayit["pnl_usdt"] - 2.4) < 1e-9, "borsanın gerçek net PnL'i yerine yaklaşık kullanıldı"
    ok("canlı _close: pozisyon açıksa gerçek emirle kapatılıyor, gerçek fiyat/PnL kaydediliyor")

    # --- _close: borsa zaten kapatmışsa (stop tetiklendi) gerçek fiyat/PnL işlem geçmişinden ---
    state, notifier, market, broker, t = canli_kur()
    p = poz(state, sym="BTCUSDT", entry=100.0, stop=95.0, side="LONG", qty=0.5)
    with patch.object(broker, "pozisyon", return_value=None), \
         patch.object(t, "_gercek_cikis_fiyati", return_value=94.8), \
         patch.object(t, "_gercek_net_pnl", return_value=-3.1):
        t._close(p, 999.0, "izleyen stop")
    kayit = state.recent_trades(1)[0]
    assert abs(kayit["exit_price"] - 94.8) < 1e-9
    assert abs(kayit["pnl_usdt"] - (-3.1)) < 1e-9
    ok("canlı _close: borsa zaten kapatmışsa gerçek dolum fiyatı işlem geçmişinden okunuyor")

    # --- mutabakat: borsada bilinmeyen pozisyon → DB'ye benimseniyor ---
    state, notifier, market, broker, t = canli_kur()
    with patch.object(broker, "pozisyon",
                       return_value={"yon": "SHORT", "miktar": 1.2, "giris": 200.0,
                                     "kaldirac": 2, "likidasyon": 0.0}), \
         patch.object(broker, "stop_var_mi", return_value={"id": 42, "stop": 210.0}):
        t.reconcile()
    pos = state.get_position("BTCUSDT")
    assert pos is not None and pos.side == "SHORT" and pos.stop_order_id == 42
    ok("canlı mutabakat: borsada bilinmeyen pozisyon DB'ye benimseniyor")

    # --- mutabakat: DB'de pozisyon var, borsada yok → kapanmış olarak işleniyor ---
    state, notifier, market, broker, t = canli_kur()
    p = poz(state, sym="BTCUSDT", entry=100.0, stop=95.0, side="LONG", qty=0.5)
    with patch.object(broker, "pozisyon", return_value=None), \
         patch.object(broker, "stop_var_mi", return_value=None), \
         patch.object(t, "_gercek_cikis_fiyati", return_value=95.2), \
         patch.object(t, "_gercek_net_pnl", return_value=-2.0):
        t.reconcile()
    assert state.get_position("BTCUSDT") is None
    kayit = state.recent_trades(1)[0]
    assert "mutabakat" in kayit["exit_reason"]
    ok("canlı mutabakat: DB'de pozisyon var ama borsada yoksa kapanmış olarak işleniyor")

    # --- veri tazeliği kapısı: bayat mumla giriş denenmiyor ---
    state, notifier, market, broker, t = canli_kur()
    row = pd.Series({"open_time": 0, "atr": 1.0})  # epoch = kesinlikle bayat
    with patch("src.futures_trader.FuturesPaperTrader._try_enter") as ust:
        t._try_enter(row, MagicMock())
    assert not ust.called, "bayat veriyle giriş denemesi engellenmeliydi"
    assert notifier.send_error.called
    ok("canlı veri tazeliği kapısı: bayat mumla giriş denenmiyor")


def test_canli_kapisi():
    """Kazara gerçek paraya geçiş İMKÂNSIZ olmalı."""
    print("\nGERÇEK PARA KAPISI")
    temel = dict(mode="futures_live", live_key="k", live_secret="s",
                 risk_pct=0.005, canli_risk_tavani=0.01)

    # Onay dizgisi yoksa AÇILMAZ.
    try:
        replace(CONFIG, **temel, canli_onay="").validate()
        raise AssertionError("onaysız canlı mod başladı!")
    except ValueError as e:
        assert "CANLI_ONAY" in str(e)
    ok("CANLI_ONAY olmadan futures_live BAŞLAMIYOR")

    # Yanlış/yaklaşık onay da kabul edilmez (tam eşleşme şart).
    for yanlis in ("evet", "EVET", "EVET_GERCEK_PARA ", "gercek_para"):
        try:
            replace(CONFIG, **temel, canli_onay=yanlis).validate()
            raise AssertionError(f"yanlış onay kabul edildi: {yanlis!r}")
        except ValueError:
            pass
    ok("yaklaşık onay dizgileri reddediliyor (tam eşleşme şart)")

    # Anahtar yoksa açılmaz.
    try:
        replace(CONFIG, mode="futures_live", live_key="", live_secret="",
                canli_onay="EVET_GERCEK_PARA").validate()
        raise AssertionError("anahtarsız canlı mod başladı!")
    except ValueError as e:
        assert "BINANCE_LIVE" in str(e)
    ok("canlı anahtar olmadan futures_live BAŞLAMIYOR")

    # Kâğıttaki risk canlıya olduğu gibi taşınmaz.
    try:
        replace(CONFIG, mode="futures_live", live_key="k", live_secret="s",
                canli_onay="EVET_GERCEK_PARA", risk_pct=0.02,
                canli_risk_tavani=0.01).validate()
        raise AssertionError("canlıda tavan üstü risk kabul edildi!")
    except ValueError as e:
        assert "tavan" in str(e)
    ok("canlıda risk tavanı (%1) aşılamıyor — kâğıt riski taşınmıyor")

    # Üç kilit de tamamsa geçer.
    replace(CONFIG, **temel, canli_onay="EVET_GERCEK_PARA").validate()
    ok("anahtar + onay + risk tavanı tamamsa canlı mod geçerli sayılıyor")

    # Kâğıt ve testnet modları bu kapıdan etkilenmemeli (regresyon).
    replace(CONFIG, mode="futures_paper").validate()
    replace(CONFIG, mode="futures_testnet", testnet_key="k",
            testnet_secret="s").validate()
    ok("kâğıt ve testnet modları kapıdan etkilenmiyor [regresyon]")

    # KAPI GEÇİLSE BİLE main.py YANLIŞ DALA DÜŞMEMELİ.
    # config.validate() yalnızca "bu ayarlar tutarlı mı" der; "bu modu
    # çalıştıracak kod var mı" DEMEZ. FuturesLiveTrader bağlandıktan sonra
    # bile futures_testnet/futures_live'ın SPOT dalına (build_broker +
    # SymbolTrader) düşmediğini, FuturesLiveTrader'a gittiğini doğrula.
    ana = (Path(__file__).resolve().parent.parent / "src" / "main.py"
           ).read_text(encoding="utf-8")
    assert "FuturesLiveTrader(" in ana,         "main.py artık FuturesLiveTrader'ı bağlamıyor"
    assert 'futures_mode = CONFIG.mode in ("futures_paper", "futures_testnet", "futures_live")' in ana,         "futures_mode üç modu da kapsamıyor — testnet/live spot dalına düşebilir"
    futures_mode_yeri = ana.index('futures_mode = CONFIG.mode in')
    broker_yeri = ana.index("= build_broker(")   # yorum değil, ÇAĞRI yeri
    assert futures_mode_yeri < broker_yeri,         "futures_mode kontrolü build_broker()'dan SONRA tanımlı"
    arasi = ana[futures_mode_yeri:broker_yeri]
    assert "elif futures_mode:" in arasi,         "build_broker() öncesinde futures_mode dalı yok — testnet/live spot dalına düşebilir"
    assert "FuturesLiveTrader(" in arasi,         "futures_mode dalı FuturesLiveTrader kurmuyor"
    ok("main.py vadeli canlı/testnet modunda FuturesLiveTrader'a bağlanıyor [sessiz spot işlemi yok]")


def test_performans_karnesi():
    """2026-09-15: kullanıcı "bot para kazandırmıyor, her şey stopla bitiyor"
    dedi. Kayda bakınca 7 işlemin 7'si de "izleyen stop"tu — ama ikisi +260 ve
    +133 USDT KAZANÇTI. Bu sistemde tek çıkış kapısı stop olduğu için kazanç
    ile zarar kayıtta AYNI görünüyordu. Bu testler o okuma hatasını yasaklar."""
    print("\nSİSTEM KARNESİ (kazanç ile zarar aynı görünmemeli)")
    from src.performans import ozet

    # --- 1) Çıkış etiketi kazancı zarardan ayırmalı
    state, notifier, trader, _ = kur()
    p = poz(state, entry=100.0, stop=120.0, qty=10.0, risk_unit=3.0)
    trader._close(p, 130.0, "izleyen stop")          # LONG 100 → 130 = KAZANÇ
    kayit = state.recent_trades(1)[0]
    assert kayit["pnl_usdt"] > 0, "kazanç işlemi zarar kaydedilmiş"
    assert "kâr kilitlendi" in kayit["exit_reason"], kayit["exit_reason"]
    assert "izleyen stop" in kayit["exit_reason"], "asıl sebep kaybolmamalı"
    ok("kazançla kapanan stop 'kâr kilitlendi' diye kaydediliyor")

    p = poz(state, entry=100.0, stop=96.0, qty=10.0, risk_unit=3.0)
    trader._close(p, 96.0, "izleyen stop")           # LONG 100 → 96 = ZARAR
    kayit = state.recent_trades(1)[0]
    assert kayit["pnl_usdt"] < 0
    assert "zarar kesildi" in kayit["exit_reason"], kayit["exit_reason"]
    ok("zararla kapanan stop 'zarar kesildi' diye kaydediliyor")

    mesajlar = [c.args[0] for c in notifier.send.call_args_list]
    assert any("KÂR KİLİTLENDİ" in m for m in mesajlar), "kazanç mesajı ayrışmıyor"
    assert any("ZARAR KESİLDİ" in m for m in mesajlar), "zarar mesajı ayrışmıyor"
    assert any("R`" in m for m in mesajlar), "R cinsinden sonuç yazılmıyor"
    ok("Telegram kapanış mesajı kazanç/zarar ve R'yi ayrı ayrı söylüyor")

    # --- 2) Beklenti matematiği: az kazanan + yüksek ödeme = pozitif sistem
    # Gerçek veri (2026-09-15): 5 zarar ~−67, 2 kazanç +261/+133 → net +59.
    gercek = [-67.95, -78.39, 260.94, 133.32, -61.01, -39.24, -88.46]
    o = ozet(gercek)
    assert o["n"] == 7 and o["kazanan"] == 2
    assert abs(o["toplam"] - 59.21) < 0.01, o["toplam"]
    assert o["beklenti"] > 0, "pozitif beklentili seri negatif hesaplandı"
    assert o["odeme_orani"] > 1.8, o["odeme_orani"]
    ok("kazanma oranı düşükken bile beklenti pozitif hesaplanıyor")

    assert o["seri"] == -3, f"son 3 zarar serisi görülmedi: {o['seri']}"
    assert o["tepeden_dusus"] < 0, "gerçekleşen K/Z geri çekilmesi ölçülmüyor"
    ok("zarar serisi ve tepeden düşüş ölçülüyor (körlük yok)")

    # --- 3) Az örnekle strateji değiştirmeye karşı açık uyarı
    assert "ANLAMSIZ" in o["yorum"], o["yorum"]
    ok("7 işlemde 'istatistiksel olarak anlamsız' uyarısı veriliyor")

    # --- 4) Sınır durumlar: boş liste ve tamamı zarar çökmemeli
    bos = ozet([])
    assert bos["n"] == 0 and bos["beklenti"] == 0 and bos["odeme_orani"] == 0
    hep = ozet([-10.0, -10.0, -10.0])
    assert hep["beklenti"] < 0 and hep["seri"] == -3
    assert hep["odeme_orani"] == 0, "kazanç yokken ödeme oranı uydurulmamalı"
    ok("boş geçmiş ve tamamı-zarar durumları çökmüyor [regresyon]")

    # --- 5) Panel karneyi gerçekten yayınlıyor mu (sessiz kaybolma yasak)
    from src import panel
    assert 'id="perf"' in panel.PAGE and 'id="bPerf"' in panel.PAGE
    assert "function perfKart" in panel.PAGE
    assert "beklenen_en_uzun_zarar" in panel.PAGE, "seri bağlamı panelde yok"
    ok("panel karneyi hem kripto hem borsa sekmesinde çiziyor")


def test_telegram_komut():
    """Telefondan pozisyon kapatabilen bir kapı açtık. Bu kapının yanlış
    açılması, botun yanlış işlem açmasından daha pahalıya patlar: yabancı
    komut çalışırsa ya da kazara /kapat gönderilirse para gider.
    Buradaki her test o kapının bir kilidini sınıyor."""
    print("\nTELEGRAM KOMUT KATMANI (yetki + onay)")
    from src.telegram_komut import OFFSET_ANAHTARI, TelegramKomut

    state, _, _, _ = kur()
    poz(state, sym="SOLUSDT", entry=100.0, stop=96.0, side="LONG")
    cfg = replace(CONFIG, telegram_token="t", telegram_chat_id="555",
                  symbols=("SOLUSDT", "BTCUSDT"))
    market = MagicMock()
    market.last_price.return_value = 104.0
    k = TelegramKomut(cfg, state, None, market)

    # --- /durum ANLIK veri vermeli (kayıtlı değil, canlı fiyat)
    d = k._isle("/durum")
    assert "SOLUSDT" in d and "104" in d, d
    assert "beklenti" in d, "durumda karne yok"
    ok("/durum canlı fiyat + karne ile anlık tablo veriyor")

    # --- Yıkıcı komut TEK BAŞINA uygulanmamalı
    c = k._isle("/kapat SOLUSDT")
    assert "onay" in c.lower(), c
    assert state.get_kv("cmd_SOLUSDT", "") == "", "onaysız komut kuyruğa düştü!"
    ok("/kapat tek başına İŞ YAPMIYOR — önce onay kodu istiyor")

    # --- Yanlış kod uygulamamalı
    assert "tutmadı" in k._isle("/onay 0000")
    assert state.get_kv("cmd_SOLUSDT", "") == "", "yanlış kodla komut geçti!"
    ok("yanlış onay kodu komutu uygulamıyor")

    # --- Doğru kod uygulamalı
    kod = k._bekleyen["kod"]
    assert "Kuyruğa" in k._isle(f"/onay {kod}")
    assert state.get_kv("cmd_SOLUSDT") == "CLOSE"
    assert k._bekleyen is None, "onaylanan işlem bekleyende kaldı (tekrar kullanılabilir)"
    ok("doğru kod komutu kuyruğa alıyor ve onayı TÜKETİYOR")

    # --- Aynı kod ikinci kez çalışmamalı (tekrar saldırısı)
    state.set_kv("cmd_SOLUSDT", "")
    assert "Bekleyen işlem yok" in k._isle(f"/onay {kod}")
    assert state.get_kv("cmd_SOLUSDT", "") == ""
    ok("kullanılmış onay kodu ikinci kez çalışmıyor [tekrar saldırısı]")

    # --- Onay süresi dolduysa uygulamamalı
    k._isle("/kapat SOLUSDT")
    kod2 = k._bekleyen["kod"]
    k._bekleyen["son"] = 0.0                     # süreyi geçmişe al
    assert "süresi doldu" in k._isle(f"/onay {kod2}")
    assert state.get_kv("cmd_SOLUSDT", "") == ""
    ok("süresi dolan onay reddediliyor")

    # --- /iptal bekleyeni gerçekten düşürmeli
    k._isle("/kapat SOLUSDT")
    k._isle("/iptal")
    assert k._bekleyen is None
    ok("/iptal bekleyen işlemi düşürüyor")

    # --- STOP: kuyruğa panelle AYNI biçimde düşmeli (bot aynı kodu işler)
    k._isle("/stop SOLUSDT 98.5")
    assert k._bekleyen["fiyat"] == 98.5
    k._isle(f"/onay {k._bekleyen['kod']}")
    assert state.get_kv("cmd_SOLUSDT") == "STOP:98.5", state.get_kv("cmd_SOLUSDT")
    ok("/stop panelle aynı komut biçimini üretiyor (tek işleyici)")

    # --- Gevşetme kullanıcıya AÇIKÇA söylenmeli (riski artırır)
    state.set_kv("cmd_SOLUSDT", "")
    g = k._isle("/stop SOLUSDT 90")
    assert "GEVŞETME" in g, g
    ok("stop gevşetmesi onay metninde 'riski artırır' diye uyarıyor")

    # --- Pozisyonsuz/bilinmeyen sembolde iş yapmamalı
    state.clear_position("SOLUSDT")
    k._bekleyen = None
    assert "açık pozisyon yok" in k._isle("/kapat SOLUSDT")
    assert "takip listesinde yok" in k._isle("/kapat YOKSUSDT")
    assert k._bekleyen is None, "geçersiz istek için onay beklentisi kuruldu"
    ok("pozisyonsuz ve bilinmeyen sembol reddediliyor (onay bile istemiyor)")

    # --- ONAY DÜĞMESİ. Kodu elle yazdırmak koruma sağlıyordu ama zahmetliydi
    # (2026-09-18: "neden 4821 yazmak zorundayım?"). Düğme aynı korumayı tek
    # dokunuşla vermeli — kolaylık, korumanın yerine değil yanına geçmeli.
    poz(state, sym="SOLUSDT", entry=100.0, stop=96.0, side="LONG")
    state.set_kv("cmd_SOLUSDT", "")
    k._bekleyen = None
    _, dug = k._cevapla("/durum")
    assert dug is None, "zararsız komuta onay düğmesi konmuş"
    cevap, dug = k._cevapla("/kapat SOLUSDT")
    assert dug, "yıkıcı komuta onay düğmesi konmamış"
    tuslar = dug["inline_keyboard"][0]
    assert tuslar[0]["callback_data"] == f"onay:{k._bekleyen['kod']}"
    assert tuslar[1]["callback_data"] == "iptal"
    ok("yıkıcı komut onay düğmesiyle geliyor, zararsız komut düğmesiz")

    # Düğmeye basmak, kod yazmakla AYNI işi yapmalı
    cagrilar = []
    k._cagir = lambda metot, **v: cagrilar.append((metot, v)) or {}
    k._dugme({"id": "cb1", "from": {"id": 555}, "data": f"onay:{k._bekleyen['kod']}",
              "message": {"message_id": 9, "chat": {"id": 555}}})
    assert state.get_kv("cmd_SOLUSDT") == "CLOSE", "düğme komutu uygulamadı"
    assert k._bekleyen is None
    ok("onay düğmesi kod yazmakla aynı işi yapıyor (tek dokunuş)")

    # Basıldıktan sonra düğme kaldırılmalı — aynı mesaja ikinci kez basılmasın
    duzenle = [v for m, v in cagrilar if m == "editMessageReplyMarkup"]
    assert duzenle and duzenle[0]["reply_markup"]["inline_keyboard"] == []
    ok("basılan düğme mesajdan kaldırılıyor (ikinci basış imkânsız)")

    # ESKİ mesajın düğmesine basmak işlem DİRİLTMEMELİ (kod tükenmişti)
    state.set_kv("cmd_SOLUSDT", "")
    k._dugme({"id": "cb2", "from": {"id": 555}, "data": "onay:9999",
              "message": {"message_id": 9, "chat": {"id": 555}}})
    assert state.get_kv("cmd_SOLUSDT", "") == "", "eski düğme komutu diriltti!"
    ok("eski/kullanılmış düğme işlem diriltmiyor")

    # YETKİ düğme yolunda da denetlenmeli: düğmeli mesaj İLETİLEBİLİR ve
    # iletilen mesajdaki düğmeye başkası basarsa callback yine bize gelir.
    poz(state, sym="SOLUSDT", entry=100.0, stop=96.0, side="LONG")
    k._bekleyen = None
    k._cevapla("/kapat SOLUSDT")
    cagrilar.clear()
    k._dugme({"id": "cb3", "from": {"id": 999}, "data": f"onay:{k._bekleyen['kod']}",
              "message": {"message_id": 11, "chat": {"id": 999}}})
    assert state.get_kv("cmd_SOLUSDT", "") == "", "YABANCI DÜĞMEYE BASTI VE ÇALIŞTI!"
    assert k._bekleyen is not None, "yabancı basış sahibin beklentisini düşürdü"
    ok("yabancının düğme basması çalışmıyor [mesaj yolundan bağımsız kapı]")
    k._bekleyen = None

    # --- SEMBOL YAZMADAN KAPATMA. 2026-09-18: "kodu nasıl alacağım, sembol
    # yazmak zorunda mıyım?" Argümansız /kapat açık pozisyonları düğme yapar.
    state.set_kv("cmd_SOLUSDT", "")
    cevap, dug = k._cevapla("/kapat")
    assert dug, "argümansız /kapat pozisyon listesi vermiyor"
    satirlar = dug["inline_keyboard"]
    assert all(len(s) == 1 for s in satirlar), "dar ekranda yan yana düğme riskli"
    assert satirlar[0][0]["callback_data"] == "kapat:SOLUSDT", satirlar[0][0]
    ok("/kapat argümansız çağrılınca pozisyonları düğme olarak listeliyor")

    # Pozisyon seçmek TEK BAŞINA kapatmamalı — ikinci kapı (onay) şart
    cagrilar.clear()
    k._dugme({"id": "cb4", "from": {"id": 555}, "data": "kapat:SOLUSDT",
              "message": {"message_id": 12, "chat": {"id": 555}}})
    assert state.get_kv("cmd_SOLUSDT", "") == "", "SEÇİM TEK BAŞINA KAPATTI!"
    assert k._bekleyen is not None, "seçimden sonra onay beklentisi kurulmadı"
    gonderilen = [v for m, v in cagrilar if m == "sendMessage"]
    assert gonderilen and gonderilen[-1].get("reply_markup"), "onay düğmesi gelmedi"
    ok("pozisyon seçmek tek başına kapatmıyor, onay düğmesi çıkarıyor")

    # Sonra onaylayınca kapanmalı — akış uçtan uca tamam
    k._dugme({"id": "cb5", "from": {"id": 555}, "data": f"onay:{k._bekleyen['kod']}",
              "message": {"message_id": 13, "chat": {"id": 555}}})
    assert state.get_kv("cmd_SOLUSDT") == "CLOSE"
    ok("seç → onayla akışı uçtan uca çalışıyor (hiç yazı yazmadan)")

    # --- /durum "şimdi stop olursa" tutarı AÇIKÇA yazmalı. Anlık kâr ile
    # karıştırılırsa yanlış karar alınır: biri kâğıt üstünde, biri cebe girecek.
    state.clear_position("SOLUSDT")
    poz(state, sym="SOLUSDT", entry=100.0, stop=104.0, side="LONG", qty=10.0)
    d2 = k._isle("/durum")
    assert "şimdi stop olursa" in d2, d2
    assert "+40.00" in d2, f"kilitli kâr (10×(104−100)) yazılmamış: {d2}"
    assert "kilitli kâr" in d2
    ok("/durum 'şimdi stop olursa ne olur' tutarını açıkça yazıyor")

    state.clear_position("SOLUSDT")
    poz(state, sym="SOLUSDT", entry=100.0, stop=96.0, side="LONG", qty=10.0)
    d3 = k._isle("/durum")
    assert "-40.00" in d3 and "göze alınan zarar" in d3, d3
    ok("stop zararda olduğunda 'göze alınan zarar' diye ayrışıyor")

    # Sonraki testler "kuyruk boş" varsayıyor — bıraktığımız komutu temizle.
    state.set_kv("cmd_SOLUSDT", "")
    k._bekleyen = None

    # --- YETKİ: yabancı sohbetin mesajı ASLA çalıştırılmamalı
    poz(state, sym="SOLUSDT", entry=100.0, stop=96.0, side="LONG")
    yazilan = []
    k2 = TelegramKomut(cfg, state, None, market)
    k2._cagir = lambda metot, **v: yazilan.append((metot, v)) or []
    guncelleme = [{"update_id": 7, "message": {"text": "/kapat SOLUSDT",
                                               "chat": {"id": 999}}}]
    cagri = {"n": 0}

    def sahte(metot, **v):
        if metot != "getUpdates":
            yazilan.append((metot, v))
            return []
        cagri["n"] += 1
        if cagri["n"] == 1:
            return guncelleme
        raise KeyboardInterrupt        # döngüyü tek turda kes
    k2._cagir = sahte
    state.set_kv(OFFSET_ANAHTARI, "7")
    try:
        k2.calistir()
    except KeyboardInterrupt:
        pass
    assert state.get_kv("cmd_SOLUSDT", "") == "", "YABANCI KOMUT ÇALIŞTI!"
    uyari = [v for m, v in yazilan if m == "sendMessage"]
    assert uyari and str(uyari[0]["chat_id"]) == "555", "sahibe uyarı gitmedi"
    assert "Yetkisiz" in uyari[0]["text"]
    ok("yabancı sohbetin komutu ÇALIŞMIYOR ve sahibe haber veriliyor")

    # --- Offset kaydı: yeniden başlayınca eski komut dirilmemeli
    assert state.get_kv(OFFSET_ANAHTARI) == "8", state.get_kv(OFFSET_ANAHTARI)
    ok("offset kaydediliyor — yeniden başlatma eski komutu tekrar işlemiyor")

    # --- MENÜ: kaydedilmezse komutlar ÇALIŞIR ama GÖRÜNMEZ. 2026-09-18'de
    # kullanıcı tam bu yüzden "sadece durum var sanırım" dedi — var olan
    # yetenek görünmeyince olmayan yetenekle aynı kapıya çıktı.
    menu = [v for m, v in yazilan if m == "setMyCommands"]
    assert menu, "komut menüsü Telegram'a hiç kaydedilmiyor"
    adlar = {c["command"] for c in menu[0]["commands"]}
    assert {"durum", "kapat", "stop", "onay", "iptal"} <= adlar, adlar
    assert all(c.get("description") for c in menu[0]["commands"]), "açıklamasız komut var"
    ok("komut menüsü Telegram'a kaydediliyor ('/' menüsünde görünür)")

    # Menü SAHİBİN sohbetine kapsamlanmalı: küresel olsaydı bota yazan herkes
    # komut listesini görürdü. Yetki tek kişideyse liste de tek kişiye.
    assert menu[0].get("scope", {}).get("type") == "chat", menu[0].get("scope")
    assert str(menu[0]["scope"]["chat_id"]) == "555", menu[0]["scope"]
    ok("menü yalnız sahibin sohbetine kapsamlanıyor (yabancı listeyi görmez)")

    # --- Kapalıyken token/chat yoksa katman hiç açılmamalı
    sessiz = TelegramKomut(replace(CONFIG, telegram_token="", telegram_chat_id=""),
                           state, None, market)
    sessiz.calistir()        # anında dönmeli, ağa çıkmamalı
    ok("token/chat_id yoksa komut katmanı hiç açılmıyor [beyaz liste yoksa kapı yok]")


def test_aksam_ozeti():
    """2026-10-03: kullanıcı "çok uyarı alıyorum, akşamdan akşama sonuca
    bakalım" dedi. Rutin bildirimler kapatıldı, sonuç tek akşam özetine
    taşındı. KRİTİK OLAN: sessizlik arızayı GİZLEMEMELİ — hata ve acil
    bildirimleri bu bayraktan bağımsız kalmalı."""
    print("\nAKŞAM ÖZETİ + SESSİZLİK (arıza gizlenmemeli)")
    from src.gunluk_rapor import DAMGA, belki_gonder, gunluk_ozet_metni

    # --- RUTİN bildirim kapalıyken gönderilmemeli
    state, _, _, t, _ = scalp_kur(scalp_bildirim=False)
    p = poz(state, sym="BTCUSDT", entry=100.0, stop=96.0, qty=10.0, risk_unit=1.0)
    t._kapat(p, 96.0, 1.0, "izleyen stop")
    assert t.notifier.send.call_count == 0, "rutin kapanış mesajı yine gitti"
    ok("SCALP_BILDIRIM=false iken rutin kapanış mesajı gitmiyor")

    state, _, _, t2, _ = scalp_kur(scalp_bildirim=True)
    p = poz(state, sym="BTCUSDT", entry=100.0, stop=96.0, qty=10.0, risk_unit=1.0)
    t2._kapat(p, 96.0, 1.0, "izleyen stop")
    assert t2.notifier.send.call_count >= 1, "bayrak açıkken mesaj gitmedi"
    ok("SCALP_BILDIRIM=true iken rutin mesaj yine çalışıyor [geri dönüş mümkün]")

    # --- ACİL/HATA bildirimi SUSTURULMAMIŞ olmalı
    kaynak = Path("src/scalp_trader.py").read_text(encoding="utf-8")
    assert "self.notifier.send_error(" in kaynak, (
        "acil/hata bildirimi de bayrağa bağlanmış olabilir — sessizlik "
        "ASLA arızayı gizlemek için kullanılmaz")
    i = kaynak.index('sonuc.get("acil")')
    assert "send_error" in kaynak[i:i + 500], "stopsuz pozisyon uyarısı susturulmuş!"
    ok("ACİL ve HATA bildirimleri bayraktan BAĞIMSIZ (hâlâ anında gidiyor)")

    # --- Özet ÜÇ kanalı birden anlatmalı
    db = Path(tempfile.mkdtemp()) / "b.db"
    bo = StateStore(db)
    bo.record_trade("AKBNK.IS", "2026-10-01T07:00:00+00:00",
                    "2026-10-01T12:00:00+00:00", 73, 66.5, 1475, "stop",
                    pnl_override=-9000.0)
    bo.record_trade("AAPL", "2026-10-01T14:00:00+00:00",
                    "2026-10-01T18:00:00+00:00", 331, 341, 3, "stop",
                    pnl_override=90.0)
    cfg = replace(CONFIG, borsa_enabled=True, borsa_db_path=db)
    metin = gunluk_ozet_metni(cfg)
    for beklenen in ("AKŞAM ÖZETİ", "KRİPTO", "GENİŞ", "BORSA", "TRY:", "USD:"):
        assert beklenen in metin, f"özette eksik: {beklenen}\n{metin}"
    assert "-9,000.00" in metin and "+90.00" in metin, "para birimleri karışmış"
    ok("akşam özeti üç kanalı birden ve borsa'yı cüzdan başına anlatıyor")

    # --- GÜNDE BİR kez: damga TEK DB'de olmalı, yoksa kanal sayısı kadar gider
    ana = StateStore(Path(tempfile.mkdtemp()) / "a.db")
    bildirim = MagicMock()
    gec = replace(cfg, daily_report_hour=0)       # saat koşulu geçsin
    assert belki_gonder(gec, ana, bildirim) is True
    assert bildirim.send.call_count == 1
    assert belki_gonder(gec, ana, bildirim) is False, "aynı akşam ikinci rapor!"
    assert bildirim.send.call_count == 1
    assert ana.get_kv(DAMGA) == datetime.now().date().isoformat()
    ok("akşam özeti günde BİR kez gidiyor (damga tek DB'de)")

    ana2 = StateStore(Path(tempfile.mkdtemp()) / "a2.db")
    b2 = MagicMock()
    erken = replace(cfg, daily_report_hour=25)    # asla gelmeyecek saat
    assert belki_gonder(erken, ana2, b2) is False
    assert b2.send.call_count == 0
    ok("rapor saati gelmeden özet gönderilmiyor")


def test_borsa_karne_para_birimi():
    """2026-10-02: borsa karnesi İKİ cüzdanı (USD + TRY) tek listede
    topluyordu ve beklenti −5.438 çıkıyordu — hiçbir şey ifade etmeyen bir
    sayı. ₺9.000 zarar ile $90 kazanç aynı ortalamaya girince "ortalama
    zarar" şişiyor, ödeme oranı ve beklenti çöpe gidiyor. Eski yorumda
    "oranlar birimsizdir" yazıyordu; o da yanlıştı."""
    print("\nBORSA KARNESİ (dolar ve lira toplanmaz)")
    from src import panel
    from src.borsa_data import para_birimi

    assert para_birimi("AKBNK.IS") == "TRY" and para_birimi("AAPL") == "USD"
    ok("para birimi sembolden doğru çıkarılıyor")

    db = Path(tempfile.mkdtemp()) / "borsa.db"
    st = StateStore(db)
    # TRY: büyük rakamlar · USD: küçük rakamlar
    st.record_trade("AKBNK.IS", "2026-10-01T07:00:00+00:00", "2026-10-01T12:00:00+00:00",
                    73.0, 66.5, 1475.0, "izleyen stop", pnl_override=-9000.0)
    st.record_trade("SISE.IS", "2026-10-01T08:00:00+00:00", "2026-10-01T13:00:00+00:00",
                    44.9, 49.2, 2077.0, "izleyen stop", pnl_override=4000.0)
    st.record_trade("AAPL", "2026-10-01T14:00:00+00:00", "2026-10-01T18:00:00+00:00",
                    331.0, 341.0, 3.0, "izleyen stop", pnl_override=90.0)
    st.record_trade("AMD", "2026-10-01T15:00:00+00:00", "2026-10-01T19:00:00+00:00",
                    521.0, 510.0, 1.0, "izleyen stop", pnl_override=-30.0)

    with patch.object(panel, "borsa_state", st):
        k = panel.build_borsa_karne()
        assert set(k) == {"TRY", "USD"}, k
        assert k["TRY"]["n"] == 2 and k["USD"]["n"] == 2
        assert abs(k["TRY"]["toplam"] - (-5000.0)) < 1e-9, k["TRY"]["toplam"]
        assert abs(k["USD"]["toplam"] - 60.0) < 1e-9, k["USD"]["toplam"]
        assert k["TRY"]["simge"] == "₺" and k["USD"]["simge"] == "$"
        ok("her cüzdan AYRI karne üretiyor, toplamlar karışmıyor")

        # Karıştırılmış sayımın ne kadar bozuk olduğunu KANITLA
        from src.performans import ozet as _o
        karisik = _o([-9000.0, 4000.0, 90.0, -30.0])
        assert karisik["ort_zarar"] > k["USD"]["ort_zarar"] * 50, (
            "karışık sayım ortalama zararı şişirmiyor — test anlamsız")
        assert karisik["odeme_orani"] != k["USD"]["odeme_orani"]
        ok("karışık sayımın ödeme oranını bozduğu kanıtlandı [regresyon]")

        d = panel.build_borsa_state()
        assert isinstance(d["performans"], dict), "/api/borsa karnesi hâlâ tek sözlük"
        assert "TRY" in d["performans"], d["performans"]
    ok("/api/borsa karnesi cüzdan başına sözlük döndürüyor")

    assert "Object.entries(d.performans" in panel.PAGE, (
        "panel JS hâlâ tek karne çiziyor")
    assert "dolar ve lira toplanmaz" in panel.PAGE, "başlık güncellenmemiş"
    ok("panel iki karneyi ayrı ayrı çiziyor")


def test_tek_ornek():
    """2026-09-30: sunucuda SEKİZ GÜN iki bot birden çalıştı (pid 8986 ve
    18256). İkisi de aynı SQLite'a yazdı, aynı log dosyasını döndürdü, aynı
    Telegram kuyruğunu çekti. Eski koruma kalp atışının YAŞINA bakıyordu:
    yarışa açıktı ve yalnızca açılışta bakıyordu. Bu testler gerçek kilidi
    sınıyor."""
    print("\nTEK ÖRNEK KİLİDİ (iki bot asla birlikte çalışmamalı)")
    from src.telegram_komut import CATISMA_ANAHTARI, CATISMA_ESIGI, TelegramKomut
    from src.tekil import KilitTutulu, TekOrnekKilidi

    yol = Path(tempfile.mkdtemp()) / "alt" / "bot.lock"
    birinci = TekOrnekKilidi(yol)
    birinci.al()
    assert yol.exists(), "kilit dosyası oluşmadı (alt dizin açılmamış olabilir)"
    ok("kilit alınıyor, gerekirse dizini kendi açıyor")

    ikinci = TekOrnekKilidi(yol)
    try:
        ikinci.al()
        raise AssertionError("İKİNCİ BOT KİLİDİ ALDI — çifte çalışma mümkün!")
    except KilitTutulu as e:
        assert str(os.getpid()) in str(e), f"sahip pid'i raporlanmıyor: {e}"
    ok("ikinci örnek kilidi ALAMIYOR ve sahibin pid'ini raporluyor")

    # Reddedilen örnek sahiplik kaydını BOZMAMALI: "w" ile açsaydı kilidi
    # alamadan içeriği sıfırlar, sahibin pid'i kaybolurdu.
    assert birinci._pid_yolu.read_text(encoding="utf-8").strip() == str(os.getpid())
    ok("reddedilen örnek sahiplik kaydını bozmuyor [regresyon]")

    # Bırakılınca devralınabilmeli — yeniden başlatma kilitli kalmamalı.
    birinci.birak()
    ucuncu = TekOrnekKilidi(yol)
    ucuncu.al()
    ok("kilit bırakılınca yeni örnek devralabiliyor (yeniden başlatma tıkanmaz)")
    ucuncu.birak()

    with TekOrnekKilidi(yol):
        dorduncu = TekOrnekKilidi(yol)
        try:
            dorduncu.al()
            raise AssertionError("with bloğu içinde kilit tutulmuyor!")
        except KilitTutulu:
            pass
    ok("with bloğu kilidi tutuyor ve çıkışta bırakıyor")

    # --- Çakışma ARTIK SESSİZ KALMAMALI. Asıl ders bu: sekiz gün boyunca
    # log saniyede bir hata bastı ama hiçbir yere alarm gitmedi.
    state, _, _, _ = kur()
    cfg = replace(CONFIG, telegram_token="t", telegram_chat_id="555")
    k = TelegramKomut(cfg, state, None, MagicMock())
    k._cagir = TelegramKomut._cagir.__get__(k)   # gerçek yöntemi kullan

    class SahteYanit:
        def __init__(self, govde): self._g = govde
        def json(self): return self._g

    catisma = {"ok": False, "description":
               "Conflict: terminated by other getUpdates request"}
    with patch("src.telegram_komut.requests.post", return_value=SahteYanit(catisma)):
        for _ in range(CATISMA_ESIGI - 1):
            k._cagir("getUpdates")
        assert state.get_kv(CATISMA_ANAHTARI, "") == "", "tek çakışmada alarm erken çaldı"
        k._cagir("getUpdates")
        assert state.get_kv(CATISMA_ANAHTARI, ""), "ÇAKIŞMA SESSİZ KALDI — 8 gün dersi"
    ok(f"{CATISMA_ESIGI} çakışmadan sonra durum kv'ye yazılıyor (sessizlik yok)")

    # Düzelince temizlenmeli, yoksa uyarı sonsuza kadar asılı kalır.
    with patch("src.telegram_komut.requests.post",
               return_value=SahteYanit({"ok": True, "result": []})):
        k._cagir("getUpdates")
    assert state.get_kv(CATISMA_ANAHTARI, "") == "", "çakışma bitince uyarı inmiyor"
    ok("çakışma bitince uyarı kendiliğinden iniyor")

    # Panel bunu bildirim sağlığından ÖNCE göstermeli (daha ağır arıza).
    from src import panel
    with patch.object(panel, "state", state):
        state.set_kv(CATISMA_ANAHTARI, "2026-09-30T13:00:00+00:00")
        state.set_kv("telegram_saglik", json.dumps({"ok": True}))
        s = panel.telegram_saglik()
        assert s["ok"] is False and "İKİ BOT" in s["sebep"], s
    ok("panel çakışmayı 'sağlıklı' kaydının ÖNÜNDE gösteriyor")


def test_bildirim_dayanikliligi():
    """2026-10-01 01:55-01:59: sunucunun DNS'i düştü. Üç sonuç doğurdu ve
    hepsi ayrı bir kusuru açığa çıkardı:
      1) O pencerede gönderilen bildirimler KALICI OLARAK KAYBOLDU (tek deneme).
      2) Sağlık kaydı 01:58'de donup kaldı; saat 04:40'ta hâlâ "Telegram
         BOZUK" diyordu, oysa arıza 3 saat önce bitmişti.
      3) Yavaş poll'lar turu 100 sn'ye çıkardı, kalp atışı bayatladı, nöbetçi
         sağlıklı botu "ölmüş" sanıp 20 saniyede bir yenisini başlattı."""
    print("\nBİLDİRİM DAYANIKLILIĞI (ağ hıçkırığı bildirimi yutmamalı)")
    from src.notifier import (DENEME_SAYISI, KALICI_HTTP, SAGLIK_ANAHTARI,
                              TelegramNotifier, saglik_canli_yaz)

    class Yanit:
        def __init__(self, kod, govde=None):
            self.status_code = kod
            self._g = govde or {}
            self.text = json.dumps(self._g)
        def json(self): return self._g

    kayitlar = {}
    n = TelegramNotifier("t", "555", saglik_yaz=lambda k, v: kayitlar.__setitem__(k, v))

    # --- GEÇİCİ arıza: yeniden denenmeli ve SONUNDA gitmeli
    cagri = {"n": 0}

    def once_patla(*a, **k):
        cagri["n"] += 1
        if cagri["n"] < 3:
            raise OSError("NameResolutionError: api.telegram.org")
        return Yanit(200, {"ok": True})

    with patch("src.notifier.requests.post", side_effect=once_patla), \
         patch("src.notifier.time.sleep"):          # testi bekletmeyelim
        assert n.send("deneme") is True, "geçici arızada bildirim kurtarılamadı"
    assert cagri["n"] == 3, f"yeniden deneme olmadı (çağrı {cagri['n']})"
    assert json.loads(kayitlar[SAGLIK_ANAHTARI])["ok"] is True
    ok(f"DNS hıçkırığında bildirim yeniden denenip gönderiliyor ({DENEME_SAYISI} hak)")

    # --- KALICI hata: denememeli, anında pes etmeli (ana döngüyü bekletmez)
    for kod in KALICI_HTTP:
        cagri["n"] = 0

        def kalici(*a, **k):
            cagri["n"] += 1
            return Yanit(kod, {"description": "Unauthorized"})

        with patch("src.notifier.requests.post", side_effect=kalici), \
             patch("src.notifier.time.sleep"):
            assert n.send("x") is False
        assert cagri["n"] == 1, f"HTTP {kod} için boşuna {cagri['n']} deneme yapıldı"
    ok(f"kalıcı hatalar {list(KALICI_HTTP)} tekrar DENENMİYOR (gecikme üretmez)")

    # --- 429/5xx GEÇİCİ sayılmalı: hız sınırı ve Telegram arızası geçer
    for kod in (429, 500, 503):
        cagri["n"] = 0

        def gecici(*a, **k):
            cagri["n"] += 1
            return Yanit(kod, {"description": "retry"})

        with patch("src.notifier.requests.post", side_effect=gecici), \
             patch("src.notifier.time.sleep"):
            n.send("x")
        assert cagri["n"] == DENEME_SAYISI, f"HTTP {kod} geçici sayılmamış"
    ok("429 ve 5xx geçici sayılıp yeniden deneniyor")

    # --- Komut katmanı sağlık kaydını TAZELEMELİ (bedava canlılık sinyali)
    saglik_canli_yaz(kayitlar.__setitem__, True)
    k2 = json.loads(kayitlar[SAGLIK_ANAHTARI])
    assert k2["ok"] is True and k2["kaynak"] == "komut-katmani"
    ok("komut katmanının getUpdates başarısı sağlık kaydını tazeliyor")

    # --- Panel BAYAT kaydı şu anki durum gibi sunmamalı
    from src import panel
    state, _, _, _ = kur()
    with patch.object(panel, "state", state):
        eski = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        state.set_kv(SAGLIK_ANAHTARI, json.dumps(
            {"ok": False, "sebep": "NameResolutionError", "ts": eski}))
        s = panel.telegram_saglik()
        assert s["bayat"] is True, "3 saatlik kayıt bayat sayılmadı"
        assert "YANSITMIYOR" in s["sebep"], s["sebep"]
        assert s["yas_sn"] > 3000
        ok("panel 3 saatlik arıza kaydını 'şu anı yansıtmıyor' diye işaretliyor")

        taze = datetime.now(timezone.utc).isoformat()
        state.set_kv(SAGLIK_ANAHTARI, json.dumps({"ok": True, "sebep": "", "ts": taze}))
        s = panel.telegram_saglik()
        assert s["bayat"] is False and s["ok"] is True
        ok("taze 'ok' kaydı bayat işaretlenmiyor")

    # --- Kalp atışı HER SEMBOLDE atmalı: yavaş tur ölüm sanılmasın
    kaynak = Path("src/main.py").read_text(encoding="utf-8")
    dongu = kaynak[kaynak.index("    while True:"):]
    govde = dongu[:dongu.index("\n        try:")]
    assert govde.count("bot_heartbeat") >= 2, (
        "kalp atışı hâlâ tur başında bir kez atıyor — yavaş tur 'ölüm' sayılır")
    assert govde.index("for t in traders") < govde.rindex("bot_heartbeat"), (
        "kalp atışı sembol döngüsünün İÇİNDE atmıyor")
    ok("kalp atışı her sembolde atıyor (yavaş tur 'bot düştü' sayılmıyor)")

    # --- Kilit reddi ERROR olmamalı: kilidin çalıştığının kanıtı, arıza değil
    assert 'log.info("Başlatılmadı' in kaynak, "kilit reddi hâlâ ERROR basıyor"
    ok("kilit reddi INFO olarak loglanıyor (hata seli üretmiyor)")


def scalp_kur(**ayar):
    """İzole scalp trader + kâğıt broker. Gerçek DB'ye DOKUNMAZ."""
    from src.kagit_broker import KagitBroker
    from src.scalp_trader import ScalpTrader
    db = Path(tempfile.mkdtemp()) / "scalp.db"
    state = StateStore(db)
    market = MagicMock()
    market.last_price.return_value = 100.0
    # GERÇEK mum verisi şart: poll() compute_indicators çağırıyor, MagicMock
    # orada çöker. Fiyat 99.5-100.5 arası salınır (kazara Donchian kırılımı
    # olmasın) ve bar aralığı 1.0 tutulur ki ATR ≈ 1.0 olsun.
    n = 300
    kapanis = [100.0 + (0.5 if i % 2 else -0.5) for i in range(n)]
    market.klines.return_value = pd.DataFrame({
        "open_time": [1_700_000_000_000 + i * 900_000 for i in range(n)],
        "open": kapanis, "close": kapanis,
        "high": [c + 0.5 for c in kapanis], "low": [c - 0.5 for c in kapanis],
        "volume": [1.0] * n,
    })
    cfg = replace(CONFIG, scalp_atr_carpani=1.0, scalp_kar_hedefi_r=1.5,
                  scalp_risk_pct=0.01, scalp_kaldirac=3.0,
                  scalp_baslangic_usdt=10_000.0,
                  **{"scalp_max_pozisyon": 2, "scalp_min_hedef_kat": 3.0, **ayar})
    broker = KagitBroker(state, cfg.scalp_baslangic_usdt, market,
                         kaldirac=cfg.scalp_kaldirac)
    notifier = MagicMock()
    return state, broker, notifier, ScalpTrader("BTCUSDT", cfg, market, state,
                                                broker, notifier), cfg


def test_scalp_kanali():
    """2026-10-01: kullanıcı kısa süreli kaldıraçlı gir-çık kanalı istedi.
    Bu kanalın hayatı komisyona bağlı: ölçtüm, 1 dakikada sürtünme 1R hedefin
    %135'i. Projenin kendi notu da "1h backtestte komisyona yenildi" diyordu.
    Bu testler maliyet kapısını, yarı kâr alımını ve frenleri sınıyor."""
    print("\nSCALP KANALI (kısa süreli kaldıraçlı gir-çık)")
    from src.kagit_broker import KagitBroker

    # --- MALİYET KAPISI: ölçülmüş ATR değerleriyle
    state, broker, notifier, t, cfg = scalp_kur()
    assert abs(t.surtunme_pct() - 0.20) < 0.001, t.surtunme_pct()
    for atr_pct, beklenen in [(0.111, False), (0.208, False),
                              (0.458, True), (1.056, True)]:
        gecer, _ = t.maliyet_kapisi(atr_pct)
        assert gecer is beklenen, f"ATR %{atr_pct} için kapı yanlış: {gecer}"
    ok("maliyet kapısı 1dk/5dk'yı reddediyor, 15dk/1sa'i geçiriyor [ölçümle]")

    # Kapı strateji sinyalinden ÖNCE olmalı: sinyal güzel olsa da maliyeti
    # karşılamayan işleme girilmemeli.
    kaynak = Path("src/scalp_trader.py").read_text(encoding="utf-8")
    poll = kaynak[kaynak.index("    def poll("):]
    assert poll.index("maliyet_kapisi") < poll.index("donchian_high"), (
        "maliyet kapısı Donchian kontrolünden SONRA — sinyal maliyeti eziyor")
    ok("maliyet kapısı strateji sinyalinden ÖNCE uygulanıyor")

    # --- GİRİŞ: stop mesafesi = ATR×çarpan, 1R kayda geçiyor
    t._ac("LONG", 100.0, 1.0)
    pos = state.get_position("BTCUSDT")
    assert pos is not None, "pozisyon açılmadı"
    assert abs(pos.risk_unit - 1.0) < 1e-9, pos.risk_unit
    assert pos.trailing_stop < pos.entry_price, "LONG stopu girişin üstünde"
    assert pos.stop_order_id, "stop emri kimliği yok — stopsuz pozisyon!"
    ok("giriş: stop ve 1R birlikte kuruluyor (stopsuz pozisyon yok)")

    # --- NOTIONAL TAVANI: dar stop sonsuz büyük pozisyon üretmemeli
    state.clear_position("BTCUSDT")
    s2, b2, _, t2, c2 = scalp_kur(scalp_max_notional_pct=0.5)
    t2._ac("LONG", 100.0, 0.01)      # çok dar stop → çok büyük miktar isteği
    p2 = s2.get_position("BTCUSDT")
    assert p2 is not None
    varlik = 10_000.0
    assert p2.qty * p2.entry_price <= varlik * 0.5 * 1.01, (
        f"notional tavanı aşıldı: {p2.qty * p2.entry_price:.0f}")
    ok("notional tavanı dar stopta pozisyonu kısıyor [hesabı tek işlem kilitlemez]")

    # --- TAVAN TÜRETİLMELİ: 2026-10-02'de sabit %150 yazılıydı ve 6 slotla
    # marj fiziken sığmıyordu — 3. pozisyondan sonrası SESSİZCE açılamazdı.
    for kald, slot in ((3.0, 2), (3.0, 6), (5.0, 6), (3.0, 10)):
        c = replace(CONFIG, scalp_kaldirac=kald, scalp_max_pozisyon=slot,
                    scalp_max_notional_pct=0)
        toplam_marj = c.scalp_notional_tavani / kald * slot
        assert abs(toplam_marj - 0.8) < 1e-9, (
            f"{kald}x/{slot} slot: tüm slotlar dolunca marj %{toplam_marj*100:.0f} "
            f"— %80 olmalıydı")
    assert replace(CONFIG, scalp_max_notional_pct=2.0).scalp_notional_tavani == 2.0
    ok("notional tavanı kaldıraç ve slottan türetiliyor [6 pozisyon gerçekten sığıyor]")

    # --- YARI KÂR ALIMI: hedefte yarısı kapanır, kalan devam eder
    state, broker, notifier, t, cfg = scalp_kur()
    t._ac("LONG", 100.0, 1.0)
    pos = state.get_position("BTCUSDT")
    ilk_qty = pos.qty
    # Girişte %0.05 kayma var → giriş 100.05, hedef 100.05+1.5 = 101.55.
    t.market.last_price.return_value = 102.0
    assert t._hedefe_vardi(pos, 102.0) is True
    t.poll()
    pos = state.get_position("BTCUSDT")
    assert pos is not None, "hedefte pozisyonun TAMAMI kapandı — yarısı kalmalıydı"
    assert abs(pos.qty - ilk_qty / 2) / ilk_qty < 0.02, f"{pos.qty} vs {ilk_qty}"
    assert t._yari_alindi() is True
    ok("hedefte YARISI kapanıyor, kalan yarı pozisyonda duruyor")

    # Kısmi kapatmada stop emri İPTAL EDİLMEMELİ — kalan yarı korumasız kalmaz
    assert pos.stop_order_id, "kısmi kapatmadan sonra stop kimliği kayboldu"
    assert "stop_id=pos.stop_order_id if tam else None" in kaynak, (
        "kısmi kapatmada stop iptal ediliyor olabilir — kalan yarı STOPSUZ kalır")
    ok("kısmi kapatmada stop KORUNUYOR (kalan yarı stopsuz kalmıyor)")

    # --- Yarı alındıktan sonra stop en az GİRİŞE çekilir: koşan yarı zarar etmez
    giris = pos.entry_price
    t._stop_guncelle(pos, 102.0, 1.0)
    pos = state.get_position("BTCUSDT")
    assert pos.trailing_stop >= giris - 1e-9, (
        f"yarı alındı ama stop girişin altında: {pos.trailing_stop} < {giris}")
    ok("yarı kâr sonrası stop en az girişe çekiliyor (koşan yarı zarar edemez)")

    # --- STOP ÇIKIŞI 1R OLMALI, 2R DEĞİL. 2026-10-02 ilk canlı scalp işlemi:
    # SOLUSDT niyet edilen $25 (1R) yerine $60 kaybetti (1.92R). Sebep: kâğıt
    # broker her zaman O ANKİ fiyattan kapatıyordu ve tur 20 saniyede bir
    # döndüğü için fiyat stopu çoktan geçmiş oluyordu. Gerçekte stop borsada
    # STOP_MARKET olarak durur ve seviyeye DOKUNDUĞU an tetiklenir.
    s7, b7, _, t7, c7 = scalp_kur()
    t7._ac("LONG", 100.0, 1.0)
    p7 = s7.get_position("BTCUSDT")
    giris7, birim7 = p7.entry_price, p7.risk_unit
    t7.market.last_price.return_value = p7.trailing_stop - 0.6   # stopu AŞTI
    t7.poll()
    assert s7.get_position("BTCUSDT") is None, "stop tetiklenmedi"
    kayit = s7.recent_trades(1)[0]
    kayipR = (giris7 - kayit["exit_price"]) / birim7
    assert kayipR < 1.25, (
        f"stop çıkışı {kayipR:.2f}R — stop SEVİYESİNDEN değil, geç fark edilen "
        f"fiyattan kapatılmış (canlıda borsa seviyeye dokununca tetikler)")
    ok(f"stop çıkışı ~1R ({kayipR:.2f}R) — stop seviyesinden kapanıyor [regresyon]")

    # --- CIRCIR: stop ASLA geri çekilmez
    once = pos.trailing_stop
    t._stop_guncelle(pos, 100.2, 1.0)                 # fiyat geri geldi
    pos = state.get_position("BTCUSDT")
    assert pos.trailing_stop >= once - 1e-12, "stop geri çekildi!"
    ok("iz süren stop geri çekilmiyor [cırcır]")

    # --- FRENLER
    # --- SANSÜRLEYEN FRENLER KÂĞITTA KAPALI OLABİLİR, CANLIDA ASLA.
    # 2026-10-02: fren tetiklendi ve kanal 11 saat veri toplamadı. Fren
    # devreye girince kötü günlerin kuyruğu ölçümden silinir ve karne
    # olduğundan iyi görünür — kâğıdın tek işi doğru ölçmek.
    s0, _, _, t0, _ = scalp_kur(scalp_max_gunluk_zarar=0, scalp_max_zarar_serisi=0)
    s0.set_kv(f"spnl_{t0._bugun()}", "-9000")          # varlığın %90'ı zarar
    s0.set_kv(f"szarar_serisi_{t0._bugun()}", "50")    # 50 üst üste zarar
    acik, sebep = t0.girisler_acik_mi()
    assert acik is True, f"fren 0'da hâlâ kapatıyor: {sebep}"
    ok("fren 0 iken kâğıt kanalı durmuyor (ölçüm sansürlenmiyor)")

    # Yapısal sınırlar fren 0 olsa da çalışmaya devam etmeli
    s0.set_kv(f"sislem_{t0._bugun()}", str(CONFIG.scalp_max_gunluk_islem))
    acik, sebep = t0.girisler_acik_mi()
    assert acik is False and "kaçak döngü" in sebep, sebep
    ok("işlem tavanı fren 0 iken de duruyor (kaçak döngü tamponu)")

    for ad, kw in (("günlük zarar", {"scalp_max_gunluk_zarar": 0}),
                   ("zarar serisi", {"scalp_max_zarar_serisi": 0})):
        try:
            replace(CONFIG, scalp_mode="live", scalp_live_key="k",
                    scalp_live_secret="s", canli_onay="EVET_GERCEK_PARA",
                    **{"scalp_max_gunluk_zarar": 0.03,
                       "scalp_max_zarar_serisi": 10, **kw}).validate()
            raise AssertionError(f"CANLIDA {ad} freni kapatılabildi!")
        except ValueError as e:
            assert "kapatılamaz" in str(e), str(e)
    ok("canlıda sansürleyen frenler KAPATILAMIYOR [kâğıt ayrıcalığı orada biter]")

    s3, b3, _, t3, c3 = scalp_kur(scalp_max_zarar_serisi=3)
    s3.set_kv(f"szarar_serisi_{t3._bugun()}", "3")
    acik, sebep = t3.girisler_acik_mi()
    assert acik is False and "üst üste zarar" in sebep, sebep
    ok("üst üste zarar freni yeni girişi durduruyor")

    s4, b4, _, t4, c4 = scalp_kur(scalp_max_gunluk_islem=2)
    s4.set_kv(f"sislem_{t4._bugun()}", "2")
    acik, sebep = t4.girisler_acik_mi()
    assert acik is False and "işlem tavanı" in sebep, sebep
    ok("günlük işlem tavanı tutuyor")

    s5, b5, _, t5, c5 = scalp_kur(scalp_max_gunluk_zarar=0.02)
    s5.set_kv(f"spnl_{t5._bugun()}", "-250")          # 10.000'in %2.5'i
    acik, sebep = t5.girisler_acik_mi()
    assert acik is False and "günlük zarar" in sebep, sebep
    ok("günlük zarar sınırı tutuyor")

    s6, b6, _, t6, c6 = scalp_kur(scalp_max_pozisyon=1)
    poz(s6, sym="BTCUSDT", entry=100.0, stop=99.0)
    acik, sebep = t6.girisler_acik_mi()
    assert acik is False and "pozisyon tavanı" in sebep, sebep
    ok("eşzamanlı pozisyon tavanı tutuyor")

    # --- MANUEL MÜDAHALE: komut kuyruğu (tek yazıcı ilkesi)
    state, broker, notifier, t, cfg = scalp_kur()
    t._ac("LONG", 100.0, 1.0)
    state.set_kv("scmd_BTCUSDT", "CLOSE")
    t.market.last_price.return_value = 100.5
    t.poll()
    assert state.get_position("BTCUSDT") is None, "manuel kapatma uygulanmadı"
    durum = state.get_kv("scmdres_BTCUSDT", "")
    assert durum.startswith("ok|"), durum
    ok("manuel KAPAT komutu uygulanıyor ve sonucu yazıyor (sessiz yutma yok)")

    t._ac("LONG", 100.0, 1.0)
    state.set_kv("scmd_BTCUSDT", "STOP:99.5")
    t.poll()
    pos = state.get_position("BTCUSDT")
    assert abs(pos.trailing_stop - 99.5) < 1e-6, pos.trailing_stop
    ok("manuel STOP komutu seviyeyi taşıyor")

    # Anında tetikleyecek stop REDDEDİLMELİ
    state.set_kv("scmd_BTCUSDT", "STOP:200")
    t.poll()
    pos = state.get_position("BTCUSDT")
    assert abs(pos.trailing_stop - 99.5) < 1e-6, "anında tetikleyen stop kabul edildi"
    assert state.get_kv("scmdres_BTCUSDT", "").startswith("red|")
    ok("anında tetikleyecek stop reddediliyor (KAPAT'a yönlendiriyor)")

    # --- BROKER DEĞİŞİMİ trader'ı etkilememeli: aynı arayüz
    from src.futures_exchange import FuturesBroker
    gerekli = ["filtreler", "bakiye_usdt", "pozisyon", "giris_ve_stop",
               "stop_tasi", "pozisyonu_kapat", "kaldirac_ayarla",
               "tek_yon_modu_mu", "emir_iptal", "stop_var_mi"]
    eksik = [m for m in gerekli if not hasattr(KagitBroker, m)]
    assert not eksik, f"KagitBroker'da eksik arayüz: {eksik}"
    eksik2 = [m for m in gerekli if not hasattr(FuturesBroker, m)]
    assert not eksik2, f"FuturesBroker'da eksik arayüz: {eksik2}"
    ok("KagitBroker ve FuturesBroker aynı arayüzü uyguluyor [canlıya geçiş konfig]")

    # --- CANLI KAPISI: alt hesap şart, ana hesapla aynı anahtar YASAK
    try:
        replace(CONFIG, scalp_mode="live", scalp_live_key="", scalp_live_secret="",
                canli_onay="EVET_GERCEK_PARA").validate()
        raise AssertionError("anahtarsız canlı scalp başladı!")
    except ValueError as e:
        assert "ALT HESABINA" in str(e), str(e)
    try:
        replace(CONFIG, scalp_mode="live", scalp_live_key="AYNI",
                scalp_live_secret="s", live_key="AYNI",
                canli_onay="EVET_GERCEK_PARA").validate()
        raise AssertionError("ana hesapla aynı anahtar kabul edildi!")
    except ValueError as e:
        assert "AYNI" in str(e), str(e)
    ok("canlı scalp ayrı alt hesap şart kılıyor [pozisyonlar birleşmesin]")

    try:
        replace(CONFIG, scalp_min_hedef_kat=0.5).validate()
        raise AssertionError("hedef < sürtünme kabul edildi!")
    except ValueError as e:
        assert "komisyon bağışlamaktır" in str(e)
    ok("hedefin sürtünmeden küçük olmasına izin verilmiyor")

    # --- Scalp ana kanala DOKUNMAMALI: ayrı DB, ayrı anahtarlar
    assert CONFIG.scalp_db_path != CONFIG.db_path
    assert "scmd_" in kaynak and "cmd_" not in kaynak.replace("scmd_", ""), (
        "scalp ana kanalın komut kuyruğunu kullanıyor olabilir")
    ok("scalp ayrı DB ve ayrı komut kuyruğu kullanıyor [ana kanal yalıtık]")

    # --- ÖLÇÜLMÜŞ AYARLAR KİLİTLİ. 2026-10-02'de 15dk·1×ATR·hedef 1.5R
    # ayarı 809 geçmiş işlemde −0.724R beklenti verdi ve canlı kâğıtta
    # günlük %3 kesiciyi tetikledi. Hangi kolun ne kattığı tek tek ölçüldü:
    #   15dk·1×ATR·1.5R·taker −0.724R → +maker −0.094R → +1sa +0.196R
    #   → +3×ATR/hedefi gevşet +0.195R
    # Doğrulama: aynı simülasyon ANA KANALIN ayarını (4sa·3×ATR·hedefsiz)
    # +0.116R buluyor; ana kanal gerçekten kârlı, model güvenilir.
    # Bu test o ölçümün kaybolmasını engeller — varsayılanlar sessizce
    # zarar eden bölgeye geri dönmesin.
    assert CONFIG.scalp_timeframe not in ("1m", "3m", "5m", "15m", "30m", "1h"), (
        f"SCALP_TIMEFRAME={CONFIG.scalp_timeframe} — 1 saat ve altı ÖLÇÜMLE "
        f"ÇÜRÜTÜLDÜ. 15dk: -0.724R (809 islem). 1sa: LONG -0.062R / SHORT "
        f"-0.121R (3144 islem, 167 gun). 1 saatin '+0.196R' sonucu 41 GUNLUK "
        f"pencereden geliyordu ve long yalniz o pencerede artiydi — asiri uyum. "
        f"4 saatte 1.8 yillik veri +0.038R ve iki yarida da artı.")
    assert CONFIG.scalp_atr_carpani >= 2.0, (
        f"ATR çarpanı {CONFIG.scalp_atr_carpani} — 1×ATR işlemlerin %75'ini "
        f"gürültüye kurban ediyordu (kazanma %25 vs 3×ATR'de %37)")
    assert CONFIG.scalp_kar_hedefi_r == 0 or CONFIG.scalp_kar_hedefi_r >= 2.5, (
        f"kâr hedefi {CONFIG.scalp_kar_hedefi_r}R — 1.5R eşiği beklentinin "
        f"üçte birini yiyor (+0.195R hedefsiz → +0.135R 1.5R'de)")
    ok("ölçülmüş scalp ayarları kilitli (zarar eden bölgeye dönüş yasak)")

    # --- KARNE POZİSYON BAZLI OLMALI. 2026-10-02: scalp hedefte yarıyı
    # kapatıp kalanı taşıyor, her kapanış `trades`'e AYRI satır yazıyor.
    # Kayıt bazlı sayım tek kazanan pozisyonu İKİ kazanç gibi gösteriyordu ve
    # iki tablo ZIT teşhis veriyordu (kayıt: kazanma %42.9/ödeme 1.02 →
    # "kazançlar küçük"; pozisyon: %27.3/2.05 → "kazanma oranı düşük").
    s8, _, _, t8, _ = scalp_kur()
    p8 = poz(s8, sym="BTCUSDT", entry=100.0, stop=96.0, qty=10.0, risk_unit=1.0)
    giris8 = p8.entry_time
    # Aynı pozisyonun iki parçası: yarısı hedefte, kalanı stopla
    s8.record_trade("BTCUSDT", giris8, "2026-10-02T06:00:00+00:00",
                    100.0, 101.5, 5.0, "kâr hedefi", side="LONG", pnl_override=30.0)
    s8.record_trade("BTCUSDT", giris8, "2026-10-02T06:30:00+00:00",
                    100.0, 103.0, 5.0, "izleyen stop", side="LONG", pnl_override=70.0)
    # Ayrı bir pozisyon: tek parçada zarar
    s8.record_trade("ETHUSDT", "2026-10-02T07:00:00+00:00", "2026-10-02T07:30:00+00:00",
                    50.0, 48.0, 10.0, "izleyen stop", side="LONG", pnl_override=-40.0)

    kayit = s8.pnl_sirali()
    pozisyon = s8.pnl_pozisyon_bazli()
    assert len(kayit) == 3, kayit
    assert len(pozisyon) == 2, f"kısmi kapanışlar birleşmedi: {pozisyon}"
    assert abs(pozisyon[0] - 100.0) < 1e-9, pozisyon
    assert abs(sum(kayit) - sum(pozisyon)) < 1e-9, "toplam K/Z değişmemeli"
    ok("kısmi kapanışlar POZİSYON bazında birleşiyor (toplam K/Z korunuyor)")

    from src.performans import ozet as _ozet
    k_karne, p_karne = _ozet(kayit), _ozet(pozisyon)
    assert k_karne["kazanma_orani"] > p_karne["kazanma_orani"], (
        "kayıt bazlı sayım kazanma oranını şişirmiyor — test anlamsız")
    assert p_karne["odeme_orani"] > k_karne["odeme_orani"], (
        "pozisyon bazlı ödeme oranı daha yüksek olmalıydı")
    ok("kayıt bazlı sayım kazanma oranını şişirip ödemeyi düşürüyor [kanıt]")

    panel_mod = __import__("src.panel", fromlist=["x"])
    # Fikstür girişleri 2026-10-02; rejim sınırını öncesine alırsak İKİSİ de
    # "yeni plan" sayılır — pozisyon bazlı sayım testi bunu bekliyor.
    cfg_eski_sinir = replace(CONFIG, scalp_rejim_baslangic="2026-10-01T00:00:00+00:00")
    with patch.object(panel_mod, "scalp_state", s8), \
         patch.object(panel_mod, "CONFIG", cfg_eski_sinir):
        ds = panel_mod.build_scalp_state()
        assert ds["performans"]["n"] == 2, (
            f"panel karnesi hâlâ kayıt bazlı: n={ds['performans']['n']}")
        assert ds["kayit_sayisi"] == 3, "kapanış kaydı sayısı gizlenmiş"
    ok("panel karnesi pozisyon bazlı, kayıt sayısı da ayrıca gösteriliyor")

    # --- REJİM AYRIMI: ölü planların faturası yeni plana kesilmez.
    # 2026-10-05: üç rejimin zararı tek karnede toplanıyordu ve kullanıcı
    # haklı olarak "sürekli zarar" okudu — oysa yürürlükteki planın kapanmış
    # işlemi SIFIRDI. Arşiv ayrı gösterilir, toplam korunur.
    cfg_ara_sinir = replace(CONFIG, scalp_rejim_baslangic="2026-10-02T06:15:00+00:00")
    yeni_p, eski_p = s8.pnl_pozisyon_bazli_rejimli("2026-10-02T06:15:00+00:00")
    # Fikstür: BTC girişi ~06:00 öncesi değil — giriş zamanına göre böl
    assert len(yeni_p) + len(eski_p) == 2
    assert abs(sum(yeni_p) + sum(eski_p) - 60.0) < 1e-9, "toplam K/Z korunmadı"
    with patch.object(panel_mod, "scalp_state", s8), \
         patch.object(panel_mod, "CONFIG", cfg_ara_sinir):
        ds = panel_mod.build_scalp_state()
        assert ds["performans"]["n"] == len(yeni_p)
        assert ds["eski_rejim"]["n"] == len(eski_p)
        assert abs(ds["eski_rejim"]["toplam"] - round(sum(eski_p), 2)) < 1e-6
    assert 'd.eski_rejim' in panel_mod.PAGE, "arşiv satırı panelde çizilmiyor"
    ok("karne rejim ayrımlı: eski planların zararı arşivde, toplam korunuyor")

    # --- YÖN KAPISI: yeni plan YALNIZ-SHORT (tarama: long 1.8 yılda −0.059R
    # sürükleme; iki yarıda da artı kalan tek varyant short'tu).
    assert CONFIG.scalp_allow_long is False, (
        "scalp_allow_long varsayılanı açılmış — tarama gerekçesi config'de")
    kaynak_sc = Path("src/scalp_trader.py").read_text(encoding="utf-8")
    assert "self.cfg.scalp_allow_long and ust" in kaynak_sc, (
        "LONG girişi scalp_allow_long kapısına bağlı değil")
    ok("yeni plan yalnız-short: LONG girişler kapıya bağlı, tek satırla geri açılır")

    # --- PANEL sözleşmesi ve sekme kimlikleri
    from src import panel
    with patch.object(panel, "scalp_state", state):
        d = panel.build_scalp_state()
        for alan in ("varlik", "bakiye", "positions", "komutlar", "performans",
                     "frenler", "trades", "timeframe", "donchian", "hedef_r"):
            assert alan in d, f"/api/scalp sözleşmesinde eksik: {alan}"
        for alan in ("zarar_serisi", "gun_islem", "min_hedef_kat", "surtunme_pct"):
            assert alan in d["frenler"], f"frenler eksik: {alan}"
    ok("/api/scalp sözleşmesi tam (frenler panelde görünür)")

    for kimlik in ("sStats", "sFrenler", "sPerf", "sPositions", "sKomutlar",
                   "sTrades", "sayfaScalp", "tabScalp"):
        assert f'id="{kimlik}"' in panel.PAGE, f"panelde {kimlik} yok"
    assert "function refreshScalp" in panel.PAGE
    assert "function scalpKapat" in panel.PAGE and "function scalpStop" in panel.PAGE
    ok("panelde SCALP sekmesi, frenler kartı ve KAPAT/STOP butonları var")

    # --- TELEGRAM: "SCALP:" öneki ONAY TURUNDA KORUNMALI.
    # En tehlikeli hata adayı buydu: BTCUSDT hem ana kanalda hem scalp'te var.
    # Önek onay turunda kaybolsa YANLIŞ POZİSYON kapanırdı.
    from src.telegram_komut import TelegramKomut
    ana, _, _, _ = kur()
    poz(ana, sym="BTCUSDT", entry=50_000.0, stop=49_000.0)     # ANA kanal pozisyonu
    scfg = replace(CONFIG, telegram_token="t", telegram_chat_id="555",
                   symbols=("BTCUSDT",), scalp_symbols=("BTCUSDT",))
    # /durum fiyatı market'ten okur; MagicMock biçimlendirmede çöker.
    tg_market = MagicMock()
    tg_market.last_price.return_value = 101.0
    tk = TelegramKomut(scfg, ana, None, tg_market, scalp_state=state)
    state.clear_position("BTCUSDT")
    t._ac("LONG", 100.0, 1.0)                                   # SCALP pozisyonu

    _, dug = tk._cevapla("/kapat SCALP:BTCUSDT")
    assert dug, "scalp kapatma onay düğmesi gelmedi"
    assert tk._bekleyen["sembol"] == "SCALP:BTCUSDT", (
        f"önek onay beklentisinde kayboldu: {tk._bekleyen['sembol']!r} — "
        f"onaylanınca ANA KANAL pozisyonu kapanırdı!")
    tk._isle(f"/onay {tk._bekleyen['kod']}")
    assert state.get_kv("scmd_BTCUSDT") == "CLOSE", "scalp kuyruğuna düşmedi"
    assert ana.get_kv("cmd_BTCUSDT", "") == "", "ANA KANALA komut SIZDI!"
    ok("SCALP: öneki onay turunda korunuyor [yanlış pozisyon kapanmıyor]")

    # Öneksiz "BTCUSDT" ANA kanalı hedeflemeli (belirsizlikte ana kanal önce)
    state.set_kv("scmd_BTCUSDT", "")
    tk._bekleyen = None
    tk._cevapla("/kapat BTCUSDT")
    assert tk._bekleyen["sembol"] == "BTCUSDT"
    tk._isle(f"/onay {tk._bekleyen['kod']}")
    assert ana.get_kv("cmd_BTCUSDT") == "CLOSE"
    assert state.get_kv("scmd_BTCUSDT", "") == "", "öneksiz komut scalp'e sızdı!"
    ok("öneksiz sembol ANA kanalı hedefliyor [belirsizlik yok]")

    # Pozisyon seçici scalp'i ÖNEKLİ listelemeli
    tk._bekleyen = None
    _, dug = tk._cevapla("/kapat")
    etiketler = [s[0]["callback_data"] for s in dug["inline_keyboard"]]
    assert "kapat:SCALP:BTCUSDT" in etiketler, etiketler
    assert "kapat:BTCUSDT" in etiketler, etiketler
    ok("/kapat listesi scalp'i önekli, ana kanalı öneksiz gösteriyor")

    d2 = tk._isle("/durum")
    assert "GENİŞ" in d2 and "şimdi stop olursa" in d2, d2[:400]
    assert "karne (yeni plan)" in d2, "rejim ayrımlı karne /durum'da yok"
    ok("/durum geniş kanal bloğunu rejim ayrımlı karneyle içeriyor")


def main() -> int:
    print("=" * 74)
    print("  KRİTİK TESTLER — geçici DB, gerçek pozisyona DOKUNULMAZ")
    print("=" * 74)
    testler = [test_geriye_uyumluluk, test_pozisyon_tavani, test_r_bildirimi,
               test_manuel_stop_ret, test_manuel_stop_sikma,
               test_manuel_stop_gevsetme, test_short, test_komut_kuyrugu,
               test_komut_sonucu, test_panel_komut_seridi, test_panel_js,
               test_panel_saat, test_deploy_teshis, test_dagitim_gerilik_alarmi,
               test_borsa_kanali,
               test_telegram_saglik, test_canli_altyapi, test_canli_trader, test_canli_kapisi,
               test_performans_karnesi, test_telegram_komut,
               test_aksam_ozeti, test_borsa_karne_para_birimi,
               test_tek_ornek, test_bildirim_dayanikliligi,
               test_scalp_kanali]
    for fn in testler:
        try:
            fn()
        except AssertionError as e:
            KALAN.append(f"{fn.__name__}: {e}")
            print(f"     ✘ BAŞARISIZ — {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            KALAN.append(f"{fn.__name__}: beklenmeyen {type(e).__name__}: {e}")
            print(f"     ✘ HATA — {fn.__name__}: {type(e).__name__}: {e}")

    print("\n" + "=" * 74)
    if KALAN:
        print(f"  {len(KALAN)} TEST BAŞARISIZ — DAĞITIM DURMALI")
        for k in KALAN:
            print(f"    • {k}")
        print("=" * 74)
        return 1
    print(f"  {GECEN}/{GECEN} TEST GEÇTİ")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
