"""Canlı izleme paneli — http://localhost:8484

    python -m src.panel

Bot sürecinden bağımsız çalışır: SQLite durumunu (WAL eşzamanlı okuma) ve
Binance canlı fiyatlarını okur; emir GÖNDERMEZ, yalnızca izler. 5 saniyede bir
kendini yeniler: açık pozisyonların anlık kâr/zararı, varlık eğrisi, işlem
geçmişi ve günlük performans.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import CONFIG, PROJECT_ROOT
from .exchange import MarketData
from .state import StateStore
from .strategy import compute_indicators

log = logging.getLogger("panel")
market = MarketData()
state = StateStore(CONFIG.db_path)

_trigger_cache: dict[str, tuple[float, float, float]] = {}  # symbol -> (ts, long_trig, short_trig)
TRIGGER_TTL = 300  # mum 4 saatte bir değişir; 5 dakikada bir yenilemek fazlasıyla yeterli


def triggers(symbol: str) -> tuple[float, float]:
    """(long tetiği, short tetiği) — Donchian bantları, önbellekli."""
    cached = _trigger_cache.get(symbol)
    if cached and time.time() - cached[0] < TRIGGER_TTL:
        return cached[1], cached[2]
    df = market.klines(symbol, CONFIG.timeframe, limit=250)
    ind = compute_indicators(df.iloc[:-1], CONFIG.strategy)
    row = ind.iloc[-1]
    hi, lo = float(row["donchian_high"]), float(row["donchian_low"])
    _trigger_cache[symbol] = (time.time(), hi, lo)
    return hi, lo


# --------------------------------------------------------------- bot kontrolü
HEARTBEAT_STALE_S = 90    # bot 30 sn'de bir atar; 3 atış kaçarsa düşmüş sayılır
CMD_RESULT_TTL_S = 120    # manuel komut sonucu panelde ne kadar asılı kalsın


def bot_alive() -> bool:
    hb = state.get_kv("bot_heartbeat")
    if not hb:
        return False
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(hb)).total_seconds()
    except ValueError:
        return False
    return age < HEARTBEAT_STALE_S


def spawn_bot() -> bool:
    """Botu ayrı, bağımsız bir süreç olarak başlatır (panel kapansa da yaşar)."""
    if bot_alive():
        return True
    try:
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        p = subprocess.Popen(
            [sys.executable, "-m", "src.main"],
            cwd=str(PROJECT_ROOT), creationflags=flags,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        state.set_kv("bot_pid", str(p.pid))
        log.info("Bot başlatıldı (pid=%s)", p.pid)
        return True
    except Exception as e:
        log.error("Bot başlatılamadı: %s", e)
        return False


def kill_bot() -> bool:
    pid = state.get_kv("bot_pid")
    if not pid:
        return False
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, timeout=10)
        else:
            os.kill(int(pid), signal.SIGTERM)
        state.set_kv("bot_heartbeat", "")
        log.info("Bot durduruldu (pid=%s)", pid)
        return True
    except Exception as e:
        log.error("Bot durdurulamadı: %s", e)
        return False


def watchdog() -> None:
    """'Başlat' dendiyse bot düşse bile geri getirir — bir daha kapanmaz."""
    while True:
        time.sleep(20)
        try:
            if state.get_kv("bot_should_run") == "1" and not bot_alive():
                log.warning("Bot düşmüş görünüyor — otomatik yeniden başlatılıyor")
                spawn_bot()
        except Exception as e:
            log.error("Watchdog hatası: %s", e)


# ------------------------------------------------------- dağıtım teşhisi/tetiği
# NEDEN VAR: otomatik güncelleme 2026-09-09'da sessizce çalışmadı ve sebebi
# bulunamadı, çünkü sunucudaki `logs/otomatik-guncelle.log` yalnızca ssh ile
# okunabiliyordu. Teşhis edilemeyen bir güvenlik kapısı, kapı değil kör noktadır:
# bu oturumda üç kez "herhalde cron çekmiştir" denip yanlış çıkıldı.
# Bu uçlar log'u ve cron durumunu HTTP üzerinden görünür kılar.
#
# GÜVENLİK: panel yalnızca Tailscale arayüzünde dinler (kur-sunucu.sh PANEL_HOST'u
# oraya sabitler), yani açık internete kapalıdır. Tetik SABİT bir betiği çalıştırır,
# dışarıdan parametre almaz — komut enjeksiyonu yüzeyi yok. Yine de bu, ağdaki
# herkese "güncellemeyi başlat" yetkisi verir; istemezsen .env'e
# DEPLOY_ENDPOINT=false yazıp kapatabilirsin.
GUNCELLE_BETIK = PROJECT_ROOT / "deploy" / "sunucu-otomatik-guncelle.sh"
GUNCELLE_LOG = PROJECT_ROOT / "logs" / "otomatik-guncelle.log"
_guncelle_calisiyor = threading.Event()


def _kabuk(*args: str, sure: int = 20) -> dict:
    """Kısa bir komut çalıştırır; çökmez, her zaman sözlük döner."""
    try:
        r = subprocess.run(list(args), cwd=str(PROJECT_ROOT), capture_output=True,
                           text=True, timeout=sure)
        return {"kod": r.returncode, "cikti": (r.stdout + r.stderr).strip()[-3000:]}
    except Exception as e:  # noqa: BLE001 — teşhis aracı, asla patlamamalı
        return {"kod": -1, "cikti": f"{type(e).__name__}: {e}"}


def _son_cron_kosusu() -> dict:
    """Cron gerçekten tetikleniyor mu? Sessizlik tek başına kanıt değildir.

    Güncelleme betiği yapacak iş yokken sessizce çıkıyor — doğru davranış, ama
    yan etkisi şu: "her şey yolunda" ile "cron hiç çalışmıyor" dışarıdan aynı
    görünüyor. 2026-09-09'da cron satırı iki gündür silinmişti ve fark edilmedi.
    Betik artık her koşuda damga bırakıyor; buradaki yaş onun kanıtı.
    """
    damga = PROJECT_ROOT / "logs" / ".son-kosu"
    try:
        ts = damga.read_text(encoding="utf-8").strip()
        yas = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
        # Cron 5 dakikada bir; 15 dakika = 3 koşu kaçtı, artık arıza sayılır.
        return {"zaman": ts, "yas_sn": int(yas), "saglikli": yas < 900}
    except FileNotFoundError:
        return {"zaman": None, "yas_sn": None, "saglikli": False,
                "not": "hiç koşmamış (damga yok) — cron kurulu mu?"}
    except Exception as e:  # noqa: BLE001
        return {"zaman": None, "yas_sn": None, "saglikli": False, "not": str(e)}


def deploy_durum() -> dict:
    satirlar: list[str] = []
    try:
        if GUNCELLE_LOG.exists():
            satirlar = GUNCELLE_LOG.read_text(
                encoding="utf-8", errors="replace").splitlines()[-80:]
    except Exception as e:  # noqa: BLE001
        satirlar = [f"log okunamadı: {e}"]
    return {
        "yerel_surum": _kabuk("git", "rev-parse", "--short", "HEAD"),
        "uzak_dal": _kabuk("git", "ls-remote", "--heads", "origin", "main", sure=45),
        "crontab": _kabuk("crontab", "-l"),
        "son_cron_kosusu": _son_cron_kosusu(),
        # Kilit tutulu mu? Guncelleme betigi `flock -n 9 || exit 0` ile cikar;
        # kilit sizarsa (bkz. 9>&- notu) her tur SESSIZCE olur. Kilidi tutan
        # sizmis bir tanitici olabilir: flock acik dosya TANIMINA baglidir ve
        # `nohup ... &` ile dogan panel/bot onu miras alabilir.
        "kilit_serbest": _kabuk(
            "flock", "-n", str(PROJECT_ROOT / "logs" / ".guncelle.lock"),
            "-c", "true"),
        "betik_var": GUNCELLE_BETIK.exists(),
        # Bu damga varsa betik ÇALIŞTI ama paneli okuyamadığı için erteledi.
        "panel_erisilemedi_damgasi": (PROJECT_ROOT / "logs" / ".panel-erisilemedi").exists(),
        "su_an_calisiyor": _guncelle_calisiyor.is_set(),
        "log_satir_sayisi": len(satirlar),
        "log": satirlar,
    }


def guncellemeyi_tetikle() -> dict:
    """Güncelleme betiğini ARKA PLANDA başlatır; sonucu log'dan izlenir.

    Eşzamanlı değil: betik testleri çalıştırıp botu yeniden başlatıyor, bu
    30-60 saniye sürüyor. İstemciyi o kadar bekletmek yerine hemen dönüyoruz;
    ilerleme /api/deploy-durum'daki log'dan okunur.
    """
    if not GUNCELLE_BETIK.exists():
        return {"ok": False, "hata": f"betik yok: {GUNCELLE_BETIK}"}
    if _guncelle_calisiyor.is_set():
        return {"ok": False, "hata": "güncelleme zaten sürüyor"}

    def calistir() -> None:
        _guncelle_calisiyor.set()
        try:
            subprocess.run(["bash", str(GUNCELLE_BETIK)], cwd=str(PROJECT_ROOT),
                           capture_output=True, text=True, timeout=600)
        except Exception as e:  # noqa: BLE001
            log.error("Güncelleme tetiklenemedi: %s", e)
        finally:
            _guncelle_calisiyor.clear()

    threading.Thread(target=calistir, daemon=True).start()
    log.info("Güncelleme elle tetiklendi (panel)")
    return {"ok": True, "not": "başlatıldı — ilerleme için /api/deploy-durum"}


def build_watchlist(open_symbols: set[str]) -> list[dict]:
    out = []
    for sym in CONFIG.symbols:
        if sym in open_symbols:
            continue  # pozisyondaysa izleme listesinde gösterilmez
        try:
            hi, lo = triggers(sym)
            price = market.last_price(sym)
        except Exception as e:
            log.warning("[%s] izleme verisi alınamadı: %s", sym, e)
            continue
        span = hi - lo
        pos_pct = ((price - lo) / span * 100) if span > 0 else 50
        out.append({
            "symbol": sym, "price": price, "long_trig": hi, "short_trig": lo,
            "long_dist": (hi / price - 1) * 100,
            "short_dist": (1 - lo / price) * 100,
            "pos_pct": max(0.0, min(100.0, pos_pct)),
        })
    out.sort(key=lambda r: min(r["long_dist"], r["short_dist"]))
    return out


def build_komutlar() -> list[dict]:
    """Manuel komutların durumu: kuyrukta mı, uygulandı mı, reddedildi mi.

    Panelin "bot 30 sn içinde uygular" yazıp kaderine terk etmesi yeterli değildi:
    bot komutu KABUL ETSE DE REDDETSE DE kuyruktan siliyor, dolayısıyla rozetin
    sönmesi "uygulandı" anlamına gelmiyordu. Bot artık sonucu `cmdres_<SEMBOL>`
    anahtarına yazıyor (bkz. FuturesPaperTrader._sonuc); burada okunup gösteriliyor.

    Pozisyon tablosunun bir kolonu DEĞİL, ayrı bir şerit: başarılı KAPAT'ta satır
    tablodan kaybolur, "açık pozisyon yok" reddinde ise hiç satır yoktur —
    ikisinde de sonucu gösterecek hücre kalmazdı.
    """
    out, simdi = [], datetime.now(timezone.utc)
    for sym in CONFIG.symbols:
        bekleyen = state.get_kv(f"cmd_{sym}", "")
        if bekleyen:
            out.append({"symbol": sym, "durum": "bekliyor",
                        "mesaj": f"{bekleyen} gönderildi — bot bir sonraki turunda uygulayacak"})
            continue
        ham = state.get_kv(f"cmdres_{sym}", "")
        if not ham:
            continue
        durum, _, kalan = ham.partition("|")
        ts, _, mesaj = kalan.partition("|")
        try:
            yas = (simdi - datetime.fromisoformat(ts)).total_seconds()
        except ValueError:
            continue
        if 0 <= yas <= CMD_RESULT_TTL_S:
            out.append({"symbol": sym, "durum": durum, "mesaj": mesaj, "yas": int(yas)})
    return out


def build_state() -> dict:
    balance = float(state.get_kv("fut_usdt", "10000.0"))
    positions = []
    equity = balance
    for p in state.all_positions():
        try:
            price = market.last_price(p.symbol)
        except Exception:
            price = p.entry_price
        upnl = (p.qty * (price - p.entry_price) if p.side == "LONG"
                else p.qty * (p.entry_price - price)) - p.funding_acc
        pos_notional = p.qty * p.entry_price
        equity += p.margin + upnl
        # --- R ilerlemesi ---
        # 1R = GİRİŞ ANINDAKİ stop mesafesi (risk_unit), bugünkü stop mesafesi
        # DEĞİL. İz süren stop yükseldikçe bugünkü mesafe daralır; onu 1R sanmak
        # kârı olduğundan büyük gösterir. (2026-09-07'de tam bu hatayı yaptım:
        # INJ'yi 4.13R sandım, gerçekte 0.96R'ydi.) Bu yüzden panelde gösteriliyor.
        birim = p.risk_unit
        r_simdi = r_hedef = None
        if birim > 0:
            r_simdi = round(((price - p.entry_price) if p.side == "LONG"
                             else (p.entry_price - price)) / birim, 2)
            seviye = CONFIG.r_notify_level
            if seviye > 0:
                r_hedef = (p.entry_price + seviye * birim if p.side == "LONG"
                           else p.entry_price - seviye * birim)
        # Stop şu an tetiklenirse gerçekleşecek K/Z — "anlık kâr" ile karıştırılmasın,
        # asıl bankaya girecek olan budur.
        stop_pnl = p.qty * ((p.trailing_stop - p.entry_price) if p.side == "LONG"
                            else (p.entry_price - p.trailing_stop))
        positions.append({
            "symbol": p.symbol, "side": p.side, "qty": p.qty,
            "entry": p.entry_price, "price": price, "stop": p.trailing_stop,
            "margin": round(p.margin, 2), "funding": round(p.funding_acc, 2),
            "upnl": round(upnl, 2),
            "upnl_pct": round(upnl / p.margin * 100, 2) if p.margin else 0,
            "notional": round(pos_notional, 2),
            "since": p.entry_time,              # TAM ISO — biçimlemeyi tarayıcı yapar
            "stop_kilit": p.stop_manual_ref,   # >0 = manuel gevşetme kilidi açık
            "r_simdi": r_simdi,                # None = eski pozisyon, risk_unit yok
            "r_hedef": r_hedef,                # bildirim eşiğine karşılık gelen fiyat
            "r_notified": p.r_notified,
            "stop_pnl": round(stop_pnl, 2),
        })

    stats = state.trade_stats()
    win_rate = stats["wins"] / stats["count"] * 100 if stats["count"] else 0
    return {
        "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bot_running": bot_alive(),
        "should_run": state.get_kv("bot_should_run") == "1",
        "max_daily_loss": CONFIG.max_daily_loss_pct * 100,
        "mode": CONFIG.mode, "leverage": CONFIG.leverage,
        "allow_short": CONFIG.allow_short,
        "symbols": list(CONFIG.symbols),
        "balance": round(balance, 2), "equity": round(equity, 2),
        "total_pnl": round(equity - 10000, 2),
        "total_pnl_pct": round((equity / 10000 - 1) * 100, 2),
        "day_realized": round(state.todays_realized_pnl(), 2),
        "n_trades": stats["count"], "win_rate": round(win_rate, 1),
        "cum_realized": round(stats["total_pnl"], 2),
        "positions": positions,
        "komutlar": build_komutlar(),
        "watchlist": build_watchlist({p["symbol"] for p in positions}),
        "trades": state.recent_trades(30),
        "equity_history": [{"t": t, "v": round(v, 2)} for t, v in state.equity_history(600)],
        "assessments": state.latest_assessments(),
        "r_events": state.recent_r_events(12),
        "max_positions": CONFIG.max_concurrent_positions,
        "r_level": CONFIG.r_notify_level,
    }


PAGE = """<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AL-SAT Paneli</title>
<style>
  :root {
    --bg:#0c0e12; --card:#14171d; --card2:#191d24; --line:#242a33;
    --ink:#e8eaed; --ink2:#9aa3ad; --mut:#5c6570;
    --up:#2fbf71; --down:#e5544b; --accent:#4d9fec; --amber:#e0a63c;
    /* STOP butonu zemini — metin daima koyu (#15181d) olduğu için bu ton AÇIK
       kalmalı. --amber'ı doğrudan kullanamayız: açık temada koyulaşıyor ve
       koyu-üstüne-koyu kontrastı 4.5'e düşüyor. */
    --amber-btn:#e0a63c;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f4f5f7; --card:#ffffff; --card2:#f0f1f4; --line:#e2e4e9;
      --ink:#15181d; --ink2:#555c66; --mut:#8a919b;
      --up:#188952; --down:#c73e36; --accent:#2472c8; --amber:#a8731a;
      --amber-btn:#f0b429; }
  }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--ink);
    font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; padding:22px; }
  .top { display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; margin-bottom:18px; }
  h1 { font-size:18px; font-weight:650; }
  .chip { font-size:11.5px; padding:3px 10px; border-radius:99px; background:var(--card2);
    color:var(--ink2); border:1px solid var(--line); }
  .chip.warn { color:var(--amber); border-color:var(--amber); }
  #clock { margin-left:auto; color:var(--mut); font-variant-numeric:tabular-nums; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:10px; margin-bottom:14px; }
  .stat { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 15px; }
  .stat .l { font-size:11px; color:var(--mut); text-transform:uppercase; letter-spacing:.05em; }
  .stat .v { font-size:22px; font-weight:650; margin-top:2px; font-variant-numeric:tabular-nums; }
  .stat .s { font-size:11.5px; color:var(--ink2); font-variant-numeric:tabular-nums; }
  .up { color:var(--up); } .down { color:var(--down); }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
    padding:14px 16px; margin-bottom:14px; }
  .card h2 { font-size:13px; color:var(--ink2); text-transform:uppercase;
    letter-spacing:.05em; font-weight:600; margin-bottom:10px; }
  table { width:100%; border-collapse:collapse; font-size:13px; font-variant-numeric:tabular-nums; }
  th { text-align:right; color:var(--mut); font-weight:500; font-size:11px;
    text-transform:uppercase; padding:4px 8px; border-bottom:1px solid var(--line); }
  td { text-align:right; padding:7px 8px; border-bottom:1px solid var(--line); }
  th:first-child, td:first-child { text-align:left; }
  tr:last-child td { border-bottom:none; }
  .side { font-size:11px; font-weight:700; padding:2px 7px; border-radius:5px; }
  .side.L { background:color-mix(in srgb, var(--up) 18%, transparent); color:var(--up); }
  .side.S { background:color-mix(in srgb, var(--down) 18%, transparent); color:var(--down); }
  .empty { color:var(--mut); padding:14px 0; text-align:center; }
  /* Manuel komut şeridi — kuyrukta/uygulandı/reddedildi. Sol kenar rengi durumu
     taşır ama TEK gösterge o değil: simge ve metin de var (renk körlüğü). */
  .cmdrow { display:flex; gap:8px; align-items:baseline; font-size:12.5px;
    background:var(--card2); border-left:3px solid var(--mut); border-radius:6px;
    padding:7px 10px; margin-bottom:6px; }
  .cmdrow b { font-size:12.5px; }
  .cmdrow .msg { color:var(--ink2); }
  .cmdrow .yas { margin-left:auto; color:var(--mut); font-size:11px; white-space:nowrap; }
  svg { display:block; width:100%; }
  .pulse { animation:pulse 2s infinite; display:inline-block; width:7px; height:7px;
    border-radius:50%; background:var(--up); margin-right:6px; vertical-align:1px; }
  .pulse.off { background:var(--mut); animation:none; }
  @keyframes pulse { 50% { opacity:.35; } }
  @media (prefers-reduced-motion: reduce) { .pulse { animation:none; } }
  .ctrl { display:flex; gap:8px; align-items:center; margin-bottom:14px; flex-wrap:wrap; }
  button { font:600 14px system-ui,-apple-system,"Segoe UI",sans-serif; cursor:pointer;
    border:none; border-radius:8px; padding:10px 22px; color:#fff; transition:opacity .15s; }
  button:hover:not(:disabled) { opacity:.87; }
  button:disabled { opacity:.35; cursor:not-allowed; }
  button:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
  #btnStart { background:var(--up); } #btnStop { background:var(--down); }
  .ctrl .state { font-size:13px; color:var(--ink2); }
  .ctrl .state b.on { color:var(--up); } .ctrl .state b.offc { color:var(--down); }
  .ctrl .note { font-size:12px; color:var(--mut); margin-left:auto; }
  button.mini { font-size:11px; font-weight:700; padding:3px 9px; border-radius:5px;
    margin-left:5px; letter-spacing:.02em; }
  button.mini.close { background:var(--down); }
  button.mini.long  { background:var(--up); padding:3px 7px; }
  button.mini.short { background:var(--down); padding:3px 7px; }
  /* STOP ikincil bir eylem: kehribar zemin + KOYU yazı. Beyaz yazı kullanılamaz —
     genel `button{color:#fff}` kuralı kehribarın üstünde okunmaz (kontrast ~1.9).
     Koyu yazıyla iki temada da 8:1 üzeri. Renk KAPAT'ın kırmızısından ayrışsın
     diye seçildi; ikisi yan yana duruyor ve yanlış tıklanmamalı. */
  button.mini.stop  { background:var(--amber-btn); color:#15181d; }
</style></head><body>
<div class="top">
  <h1><span class="pulse" id="pulse"></span>AL-SAT Paneli</h1>
  <span class="chip" id="mode"></span>
  <span class="chip" id="lev"></span>
  <span class="chip warn" id="paper">PAPER — gerçek para riski yok</span>
  <span id="clock"></span>
</div>
<div class="ctrl">
  <button id="btnStart" onclick="ctrl('start')">▶ BAŞLAT</button>
  <button id="btnStop" onclick="ctrl('stop')">■ DURDUR</button>
  <span class="state" id="botstate"></span>
  <span class="note" id="dailynote"></span>
</div>
<div class="grid" id="stats"></div>
<div class="card"><h2>Ön Değerlendirme — her mum kapanışında 5 araçlı analiz (incelemesiz giriş yok)</h2><div id="assess"></div></div>
<div class="card"><h2>Açık Pozisyonlar — anlık kâr/zarar</h2><div id="komutlar"></div><div id="positions"></div></div>
<div class="card"><h2>İzleme Listesi — tetiğe uzaklık</h2><div id="watch"></div></div>
<div class="card"><h2>Varlık Eğrisi</h2><div id="chart"><div class="empty">Veri birikiyor…</div></div></div>
<div class="card"><h2>Kâr Kilometre Taşları <span id="rbaslik" style="font-weight:400;color:var(--mut);font-size:13px"></span></h2><div id="revents"></div></div>
<div class="card"><h2>Son İşlemler</h2><div id="trades"></div></div>
<script>
const $ = id => document.getElementById(id);
const money = v => (v<0?"−$":"$") + Math.abs(v).toLocaleString("tr-TR",{minimumFractionDigits:2,maximumFractionDigits:2});
const cls = v => v > 0 ? "up" : v < 0 ? "down" : "";
const sign = v => (v>0?"+":"") + v.toLocaleString("tr-TR",{maximumFractionDigits:2});
const kisa = v => (+v).toLocaleString("tr-TR",{maximumSignificantDigits:6});
// ZAMAN. Sunucu her seyi UTC yazar; GOSTERIM tarayicinin saat diliminde yapilir.
// Once panel "21:35 UTC" diyordu, kullanici saatine bakip 00:35 goruyordu ve
// aradaki 3 saati kafadan ekliyordu. Sunucu saati dogruydu, sunum yanlisti.
const _tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
const _dt = s => {
  if (!s) return null;
  // Ofset yoksa UTC varsay; yoksa tarayici YEREL sanip 3 saat kaydirir.
  const t = (/[Zz]|[+-]\\d{2}:?\\d{2}$/.test(s) ? s : s + "Z").replace(" ", "T");
  const d = new Date(t);
  return isNaN(d) ? null : d;
};
const saat = s => { const d = _dt(s); return d ? d.toLocaleTimeString("tr-TR",
  {hour:"2-digit",minute:"2-digit"}) : "—"; };
const gunSaat = s => { const d = _dt(s); return d ? d.toLocaleString("tr-TR",
  {day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}) : "—"; };
// Bot mesajları innerHTML'e giriyor; içlerinde ham kullanıcı girdisi (reddedilen
// stop dizgisi) olabildiği için kaçırılır.
const kacir = s => String(s ?? "").replace(/[&<>"]/g, c =>
  ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;" }[c]));
// R ilerlemesi. 1R = GİRİŞ anındaki stop mesafesi; iz süren stop yükseldikçe
// bugünkü mesafe daralır ve onu 1R sanmak kârı olduğundan büyük gösterir.
// risk_unit'i olmayan (özellikten önce açılmış) pozisyonlarda "—" gösterilir,
// tahmin uydurulmaz.
function rHucre(p) {
  if (p.r_simdi === null || p.r_simdi === undefined)
    return `<span style="color:var(--mut)" title="Bu pozisyon R takibinden önce açıldı">—</span>`;
  const ulasti = p.r_notified > 0 && p.r_simdi >= p.r_notified;
  const renk = ulasti ? "var(--up)" : p.r_simdi > 0 ? "var(--ink)" : "var(--mut)";
  const hedef = p.r_hedef
    ? `<div style="font-size:10px;color:var(--mut)">hedef $${kisa(p.r_hedef)}</div>` : "";
  return `<b style="color:${renk}">${sign(p.r_simdi)}R</b>${ulasti ? " 🎯" : ""}${hedef}`;
}

async function refresh() {
  let d;
  try { d = await (await fetch("/api/state")).json(); }
  catch { $("clock").textContent = "bağlantı koptu — yeniden denenecek"; return; }

  { const d0 = _dt(d.now);
    $("clock").textContent = d0 ? d0.toLocaleTimeString("tr-TR") : "—";
    $("clock").title = `Sunucu UTC: ${(d.now||"").slice(11,19)} · gösterim: ${_tz}`; }
  $("pulse").className = "pulse" + (d.bot_running ? "" : " off");
  $("btnStart").disabled = d.bot_running;
  $("btnStop").disabled = !d.bot_running;
  $("botstate").innerHTML = d.bot_running
    ? "Bot <b class='on'>ÇALIŞIYOR</b> — otomatik yeniden başlatma açık, kapanmaz"
    : (d.should_run ? "Bot <b class='offc'>DÜŞTÜ</b> — otomatik başlatılıyor…"
                    : "Bot <b class='offc'>DURDURULDU</b>");
  $("dailynote").textContent = "Günlük sermaye stopu: %" + d.max_daily_loss +
    " zararda tüm pozisyonlar kapanır";
  $("mode").textContent = "mod: " + d.mode;
  $("lev").textContent = d.leverage + "x kaldıraç" + (d.allow_short ? " · long+short" : " · sadece long");
  $("paper").style.display = d.mode.includes("paper") || d.mode === "dry_run" ? "" : "none";

  $("stats").innerHTML = [
    ["Toplam Varlık", money(d.equity), sign(d.total_pnl_pct) + "% başlangıçtan", d.total_pnl],
    ["Serbest Bakiye", money(d.balance), "marjin dışı", 0],
    ["Bugün Gerçekleşen", money(d.day_realized), "kapanan işlemlerden", d.day_realized],
    ["Toplam İşlem", d.n_trades, "kazanma %" + d.win_rate, 0],
    ["Kümülatif PnL", money(d.cum_realized), "kapanmış işlemler", d.cum_realized],
  ].map(([l,v,s,c]) =>
    `<div class="stat"><div class="l">${l}</div><div class="v ${cls(c)}">${v}</div><div class="s">${s}</div></div>`
  ).join("");

  $("assess").innerHTML = d.assessments.length ? "<table><tr>" +
    "<th>Parite</th><th>Karar</th><th>Skor</th><th style='text-align:left'>Araç Oyları</th><th>Zaman</th></tr>" +
    d.assessments.map(a => {
      const col = a.decision === "LONG-UYGUN" ? "up" : a.decision === "SHORT-UYGUN" ? "down" : "";
      const votes = a.veto ? `<span class="down">VETO: ${a.veto}</span>`
        : (a.votes||[]).map(v => `${v.tool} <b class="${cls(v.score)}">${v.score>0?"+":""}${v.score}</b>`).join(" · ");
      return `<tr><td><b>${a.symbol}</b></td>
        <td class="${col}"><b>${a.decision}</b></td>
        <td class="${cls(a.score)}">${a.score>0?"+":""}${a.score}</td>
        <td style="text-align:left;color:var(--ink2);font-size:12px">${votes}</td>
        <td style="color:var(--mut)">${saat(a.ts)}</td></tr>`;
    }).join("") + "</table>"
    : `<div class="empty">İlk mum kapanışı bekleniyor — analiz her 4 saatte bir yenilenir</div>`;

  // Manuel komut şeridi. "Rozet söndü = uygulandı" YANLIŞTI (bot reddettiğinde de
  // kuyruktan siliyor), o yüzden bot artık sonucu yazıyor ve burada gösteriliyor.
  $("komutlar").innerHTML = (d.komutlar || []).map(k => {
    const s = k.durum === "bekliyor" ? ["⏳", "var(--amber)"]
            : k.durum === "ok"       ? ["✅", "var(--up)"]
            : k.durum === "red"      ? ["⛔", "var(--down)"]
            :                          ["ℹ️", "var(--mut)"];
    const yas = k.durum === "bekliyor" ? "" : `<span class="yas">${k.yas} sn önce</span>`;
    return `<div class="cmdrow" style="border-left-color:${s[1]}">
      <span>${s[0]}</span><b>${kacir(k.symbol)}</b>
      <span class="msg">${kacir(k.mesaj)}</span>${yas}</div>`;
  }).join("");

  $("positions").innerHTML = d.positions.length ? "<table><tr>" +
    "<th>Parite</th><th>Yön</th><th>Giriş</th><th>Anlık</th><th>Stop</th>" +
    "<th title='Giriş anındaki stop mesafesi 1R kabul edilir'>R</th>" +
    "<th title='Stop şu an tetiklenirse bankaya girecek gerçek tutar'>Stop olursa</th>" +
    "<th>Marjin</th><th>Funding</th><th>Anlık PnL</th><th>%</th><th>Manuel</th></tr>" +
    d.positions.map(p => `<tr>
      <td><b>${p.symbol}</b> <span style="color:var(--mut);font-size:11px">${gunSaat(p.since)}</span></td>
      <td><span class="side ${p.side[0]}">${p.side}</span></td>
      <td>$${p.entry.toLocaleString("tr-TR")}</td>
      <td>$${p.price.toLocaleString("tr-TR")}</td>
      <td>$${p.stop.toLocaleString("tr-TR",{maximumFractionDigits:6})}${
          p.stop_kilit > 0 ? ` <span title="Manuel gevşetme kilidi açık — iz süren stop durduruldu" style="color:var(--amber)">🔓</span>` : ""}</td>
      <td>${rHucre(p)}</td>
      <td class="${cls(p.stop_pnl)}">${money(p.stop_pnl)}</td>
      <td>${money(p.margin)}</td><td>${money(p.funding)}</td>
      <td class="${cls(p.upnl)}"><b>${money(p.upnl)}</b></td>
      <td class="${cls(p.upnl)}">${sign(p.upnl_pct)}%</td>
      <td><button class="mini stop" onclick="stopDegistir('${p.symbol}','${p.side}',${p.stop},${p.price},${p.entry},${p.qty})">STOP</button>
          <button class="mini close" onclick="manuel('close','${p.symbol}',${p.upnl.toFixed(2)})">KAPAT</button></td></tr>`).join("") + "</table>"
    : `<div class="empty">Açık pozisyon yok — bot sinyal bekliyor (${d.symbols.join(", ")})</div>`;

  $("watch").innerHTML = d.watchlist.length ? "<table><tr>" +
    "<th>Parite</th><th>Fiyat</th><th style='text-align:center'>SHORT ← konum → LONG</th>" +
    "<th>SHORT tetik</th><th>LONG tetik</th><th>Durum</th></tr>" +
    d.watchlist.map(w => {
      const nearest = Math.min(w.long_dist, w.short_dist);
      const tag = nearest < 0.3 ? "<span style='color:var(--amber);font-weight:700'>TETİKTE</span>"
        : nearest < 1.5 ? "<span style='color:var(--accent);font-weight:600'>YAKIN</span>"
        : "<span style='color:var(--mut)'>bekliyor</span>";
      return `<tr>
        <td><b>${w.symbol.replace("USDT","")}</b><span style="color:var(--mut);font-size:11px">USDT</span>
          <button class="mini long" onclick="manuel('long','${w.symbol}')">L</button>
          <button class="mini short" onclick="manuel('short','${w.symbol}')">S</button></td>
        <td>$${w.price.toLocaleString("tr-TR",{maximumFractionDigits:2})}</td>
        <td style="width:34%">
          <div style="position:relative;height:6px;background:var(--card2);border-radius:3px">
            <div style="position:absolute;left:${w.pos_pct}%;top:-4px;width:3px;height:14px;
              background:var(--ink);border-radius:2px;transform:translateX(-1.5px)"></div>
          </div></td>
        <td class="down">$${w.short_trig.toLocaleString("tr-TR",{maximumFractionDigits:2})}
          <span style="color:var(--mut);font-size:11px">%${w.short_dist.toFixed(2)}</span></td>
        <td class="up">$${w.long_trig.toLocaleString("tr-TR",{maximumFractionDigits:2})}
          <span style="color:var(--mut);font-size:11px">%${w.long_dist.toFixed(2)}</span></td>
        <td>${tag}</td></tr>`;
    }).join("") + "</table>"
    : `<div class="empty">Tüm pariteler pozisyonda</div>`;

  if (d.equity_history.length > 1) {
    const H = 160, W = 900, pts = d.equity_history;
    const vs = pts.map(p => p.v), mn = Math.min(...vs, 9990), mx = Math.max(...vs, 10010);
    const X = i => 40 + i / (pts.length - 1) * (W - 50);
    const Y = v => 8 + (mx - v) / (mx - mn) * (H - 30);
    const path = pts.map((p,i) => (i?"L":"M") + X(i).toFixed(1) + " " + Y(p.v).toFixed(1)).join("");
    const base = Y(10000);
    $("chart").innerHTML = `<svg viewBox="0 0 ${W} ${H}">
      <line x1="40" x2="${W-10}" y1="${base}" y2="${base}" stroke="var(--line)" stroke-dasharray="4 4"/>
      <text x="36" y="${base+4}" text-anchor="end" font-size="10" fill="var(--mut)">10k</text>
      <path d="${path}" fill="none" stroke="var(--accent)" stroke-width="2"/>
      <circle cx="${X(pts.length-1)}" cy="${Y(vs.at(-1))}" r="3.5" fill="var(--accent)"/>
      <text x="${X(pts.length-1)-8}" y="${Y(vs.at(-1))-8}" text-anchor="end" font-size="11"
        fill="var(--ink)" font-weight="600">$${vs.at(-1).toLocaleString("tr-TR")}</text></svg>`;
  }

  $("rbaslik").textContent = `— ${d.r_level}R'de bildirim gider, pozisyon kapanmaz`;
  $("revents").innerHTML = (d.r_events && d.r_events.length) ? "<table><tr>" +
    "<th>Parite</th><th>Yön</th><th>Eşik</th><th>Gerçek</th><th>Fiyat</th>" +
    "<th>O anki kâr</th><th>Stop kilidi</th><th>Zaman</th></tr>" +
    d.r_events.map(e => `<tr>
      <td><b>${e.symbol}</b></td>
      <td><span class="side ${(e.side||"LONG")[0]}">${e.side||"LONG"}</span></td>
      <td>${(+e.r_level).toFixed(0)}R</td>
      <td>${(+e.r_actual).toFixed(1)}R</td>
      <td>$${(+e.price).toLocaleString("tr-TR")}</td>
      <td class="${cls(e.upnl_usdt)}"><b>${money(e.upnl_usdt)}</b></td>
      <td class="${cls(e.locked_usdt)}">${money(e.locked_usdt)}</td>
      <td style="color:var(--mut)">${gunSaat(e.ts)}</td></tr>`).join("") + "</table>"
    : `<div class="empty">Henüz ${d.r_level}R kârına ulaşan pozisyon olmadı</div>`;

  $("trades").innerHTML = d.trades.length ? "<table><tr>" +
    "<th>Parite</th><th>Yön</th><th>Giriş → Çıkış</th><th>Net PnL</th><th>%</th><th>Neden</th><th>Kapanış</th></tr>" +
    d.trades.map(t => `<tr>
      <td><b>${t.symbol}</b></td>
      <td><span class="side ${(t.side||"LONG")[0]}">${t.side||"LONG"}</span></td>
      <td>$${(+t.entry_price).toLocaleString("tr-TR")} → $${(+t.exit_price).toLocaleString("tr-TR")}</td>
      <td class="${cls(t.pnl_usdt)}"><b>${money(t.pnl_usdt)}</b></td>
      <td class="${cls(t.pnl_usdt)}">${sign(t.pnl_pct)}%</td>
      <td style="color:var(--ink2)">${t.exit_reason}</td>
      <td style="color:var(--mut)">${gunSaat(t.exit_time)}</td></tr>`).join("") + "</table>"
    : `<div class="empty">Henüz kapanan işlem yok</div>`;
}
async function stopDegistir(sembol, yon, stop, fiyat, giris, qty) {
  const isim = sembol.replace("USDT","");
  const uzun = yon === "LONG";
  const ham = prompt(
    `${isim} ${yon} — yeni stop seviyesi\n\n` +
    `Giriş  : $${giris}\nAnlık  : $${fiyat}\nŞu anki stop: $${stop}\n\n` +
    (uzun ? `LONG: stop anlık fiyatın ALTINDA olmalı.` : `SHORT: stop anlık fiyatın ÜSTÜNDE olmalı.`),
    String(stop));
  if (ham === null) return;
  const yeni = parseFloat(String(ham).replace(",", "."));
  if (!(yeni > 0)) { alert("Geçersiz sayı."); return; }
  if (uzun ? yeni >= fiyat : yeni <= fiyat) {
    alert(`Bu seviye pozisyonu ANINDA kapatır.\nİstediğin buysa KAPAT düğmesini kullan.`);
    return;
  }
  const sik = uzun ? yeni > stop : yeni < stop;
  const risk = qty * (uzun ? fiyat - yeni : yeni - fiyat);
  const kilit = qty * (uzun ? yeni - giris : giris - yeni);
  const ozet = `Risktekiler: ${risk.toFixed(2)} USDT\n` +
               `Stop'un garantilediği: ${kilit >= 0 ? "+" : ""}${kilit.toFixed(2)} USDT`;
  const soru = sik
    ? `${isim}: stop SIKILIYOR\n\n$${stop} → $${yeni}\n\n${ozet}\n\n` +
      `İz süren stop buradan yukarı devam edecek.`
    : `⚠️ ${isim}: stop GEVŞETİLİYOR\n\n$${stop} → $${yeni}\n\n${ozet}\n\n` +
      `DİKKAT: Zararı büyütme iznidir. Botun kendine yasakladığı hareket bu —\n` +
      `iz süren stop asla geri gevşemez.\n\n` +
      `İz sürme DURDURULACAK; işlem verdiğin payı geri kazanıp aday\n` +
      `$${(2*stop - yeni).toFixed(6).replace(/0+$/,"").replace(/\\.$/,"")} seviyesini geçince kendiliğinden devam eder.\n\nEmin misin?`;
  if (!confirm(soru)) return;
  try {
    await fetch(`/api/manual/stop/${sembol}/${yeni}`, { method: "POST" });
    alert(`Komut kuyruğa alındı.

Sonucu tahmin etmene gerek yok: pozisyon tablosunun üstündeki şeritte ⏳ bekliyor olarak görünür, bot uygulayınca ✅ ya da ⛔ (sebebiyle) olur. Tur süresi ~1 dakika.`);
  } catch { alert("Komut gönderilemedi."); }
  setTimeout(refresh, 2000);
}

async function manuel(eylem, sembol, pnl) {
  const isim = sembol.replace("USDT","");
  let soru;
  if (eylem === "close") {
    soru = `${isim} pozisyonunu ŞİMDİ kapat?\n\n` +
           `Anlık kâr/zarar: ${pnl >= 0 ? "+" : ""}${pnl} USDT\n\n` +
           `Not: Stratejinin kârı, kazanan işlemlerin uzun sürmesinden gelir.\n` +
           `Erken kapatmak uzun vadeli getiriyi düşürür.`;
  } else {
    soru = `${isim} için MANUEL ${eylem.toUpperCase()} pozisyon aç?\n\n` +
           `Bot sinyal vermedi — bu senin kararın.\n` +
           `Risk yönetimi ve ATR stop yine uygulanacak.`;
  }
  if (!confirm(soru)) return;
  try {
    await fetch(`/api/manual/${eylem}/${sembol}`, { method: "POST" });
    alert(`Komut kuyruğa alındı.

Sonucu tahmin etmene gerek yok: pozisyon tablosunun üstündeki şeritte ⏳ bekliyor olarak görünür, bot uygulayınca ✅ ya da ⛔ (sebebiyle) olur. Tur süresi ~1 dakika.`);
  } catch { alert("Komut gönderilemedi."); }
  setTimeout(refresh, 2000);
}

async function ctrl(action) {
  $("btnStart").disabled = $("btnStop").disabled = true;
  $("botstate").textContent = action === "start" ? "Başlatılıyor…" : "Durduruluyor…";
  try { await fetch("/api/" + action, { method: "POST" }); } catch {}
  setTimeout(refresh, 1500);
}
refresh();
setInterval(refresh, 5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/state":
            try:
                body = json.dumps(build_state()).encode("utf-8")
                self._send(200, "application/json", body)
            except Exception as e:
                log.error("state hatası: %s", e)
                self._send(500, "application/json", b'{"error":"state"}')
        elif self.path == "/api/deploy-durum":
            # Teşhis ucu: cron kurulu mu, betik çalışmış mı, neden ertelemiş.
            try:
                body = json.dumps(deploy_durum(), ensure_ascii=False).encode("utf-8")
                self._send(200, "application/json", body)
            except Exception as e:  # noqa: BLE001
                log.error("deploy-durum hatası: %s", e)
                self._send(500, "application/json", b'{"error":"deploy-durum"}')
        elif self.path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        else:
            self._send(404, "text/plain", b"404")

    def do_POST(self):
        # Manuel komutlar: panel pozisyona DOKUNMAZ, komutu kuyruğa yazar;
        # bot bir sonraki turunda (~1 dk) uygular. Tek yazıcı ilkesi korunur.
        # Sonuç cmdres_<SEMBOL>'e yazılır ve panelde şerit olarak gösterilir.
        if self.path.startswith("/api/manual/"):
            parts = self.path.split("/")  # ['', 'api', 'manual', <eylem>, <sembol>]
            if len(parts) == 5:
                eylem, sembol = parts[3].upper(), parts[4].upper()
                gecerli = {"CLOSE": "CLOSE", "LONG": "OPEN_LONG", "SHORT": "OPEN_SHORT"}
                if eylem in gecerli and sembol in CONFIG.symbols:
                    state.set_kv(f"cmd_{sembol}", gecerli[eylem])
                    log.info("Manuel komut kuyruğa alındı: %s %s", sembol, gecerli[eylem])
                    self._send(200, "application/json",
                               json.dumps({"ok": True, "queued": gecerli[eylem]}).encode())
                    return
            # /api/manual/stop/<sembol>/<fiyat> — geçerlilik denetimi BOTTA yapılır
            # (tek yazıcı ilkesi: panel pozisyonu okumaz, yalnızca komut bırakır).
            if len(parts) == 6 and parts[3].upper() == "STOP":
                sembol = parts[4].upper()
                try:
                    fiyat = float(parts[5])
                except ValueError:
                    fiyat = 0.0
                if sembol in CONFIG.symbols and fiyat > 0:
                    state.set_kv(f"cmd_{sembol}", f"STOP:{fiyat!r}")
                    log.info("Manuel STOP kuyruğa alındı: %s → %s", sembol, fiyat)
                    self._send(200, "application/json",
                               json.dumps({"ok": True, "queued": f"STOP:{fiyat}"}).encode())
                    return
            self._send(400, "application/json", b'{"ok":false}')
            return

        if self.path == "/api/guncelle":
            if not CONFIG.deploy_endpoint:
                self._send(403, "application/json",
                           b'{"ok":false,"hata":"DEPLOY_ENDPOINT=false"}')
                return
            self._send(200, "application/json",
                       json.dumps(guncellemeyi_tetikle(), ensure_ascii=False).encode())
            return

        if self.path == "/api/start":
            state.set_kv("bot_should_run", "1")
            ok = spawn_bot()
            self._send(200, "application/json", json.dumps({"ok": ok}).encode())
        elif self.path == "/api/stop":
            state.set_kv("bot_should_run", "0")
            ok = kill_bot()
            self._send(200, "application/json", json.dumps({"ok": ok}).encode())
        else:
            self._send(404, "text/plain", b"404")

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # erişim loglarını sustur
        pass


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    threading.Thread(target=watchdog, daemon=True).start()
    addr = (CONFIG.panel_host, CONFIG.panel_port)
    server = ThreadingHTTPServer(addr, Handler)
    print(f"Panel hazır: http://{CONFIG.panel_host}:{CONFIG.panel_port}  (Ctrl+C ile durdurun)")
    print("Sayfadaki BAŞLAT/DURDUR butonlarıyla botu yönetebilirsiniz.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
