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

    python -m tests.kritik_testler
"""
from __future__ import annotations

import json
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
from src.futures_trader import FuturesPaperTrader  # noqa: E402
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


def main() -> int:
    print("=" * 74)
    print("  KRİTİK TESTLER — geçici DB, gerçek pozisyona DOKUNULMAZ")
    print("=" * 74)
    testler = [test_geriye_uyumluluk, test_pozisyon_tavani, test_r_bildirimi,
               test_manuel_stop_ret, test_manuel_stop_sikma,
               test_manuel_stop_gevsetme, test_short, test_komut_kuyrugu,
               test_komut_sonucu, test_panel_komut_seridi, test_panel_js,
               test_panel_saat, test_deploy_teshis, test_borsa_kanali,
               test_telegram_saglik]
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
