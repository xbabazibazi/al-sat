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


def main() -> int:
    print("=" * 74)
    print("  KRİTİK TESTLER — geçici DB, gerçek pozisyona DOKUNULMAZ")
    print("=" * 74)
    testler = [test_geriye_uyumluluk, test_pozisyon_tavani, test_r_bildirimi,
               test_manuel_stop_ret, test_manuel_stop_sikma,
               test_manuel_stop_gevsetme, test_short, test_komut_kuyrugu,
               test_komut_sonucu, test_panel_komut_seridi, test_panel_js,
               test_panel_saat]
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
