"""Merkezi yapılandırma: .env + varsayılanlar.

Tüm ayarlar tek yerden okunur; strateji, canlı bot ve backtest aynı
değerleri kullanır ki backtest ile canlı davranış birbirinden sapmasın.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(PROJECT_ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class StrategyParams:
    """Strateji parametreleri — backtest ve canlı bot İKİSİ DE bunu kullanır."""
    ema_period: int = 200
    rsi_period: int = 14
    atr_period: int = 14
    donchian_period: int = int(_env("DONCHIAN_PERIOD", "20"))  # kırılım kanalı (10=aktif, 20=sabırlı)
    atr_multiplier: float = float(_env("ATR_MULTIPLIER", "3.0"))  # 2022-2026 taramasında sağlam bölge
    rsi_max_entry: float = 80.0        # aşırı alımda (blow-off) girişleri engelle
    warmup_bars: int = 220             # EMA200'ün oturması için gereken minimum bar
    # Günlük trend teyidi: yalnızca GÜNLÜK kapanış da günlük EMA'nın üzerindeyse gir.
    # A/B testi (2022-2026): SOL'da belirgin fayda, BTC/ETH'de belirgin ZARAR →
    # tutarsız olduğu için varsayılan KAPALI. Denemek isteyen .env'den açabilir.
    use_daily_filter: bool = _env("USE_DAILY_FILTER", "false").lower() == "true"
    daily_ema_period: int = 200


@dataclass(frozen=True)
class Config:
    # dry_run | testnet | live | futures_paper | futures_testnet | futures_live
    mode: str = _env("BOT_MODE", "dry_run").lower()

    # CANLI PARA KAPISI — kazara açılmayı imkânsız kılar.
    # futures_live modu YALNIZCA bu değer tam olarak "EVET_GERCEK_PARA" ise
    # başlar. Tek bir .env satırının yanlışlıkla değişmesi ya da eski bir
    # yedeğin geri yüklenmesi gerçek parayı riske atmasın diye ikinci bir
    # bilinçli onay şarttır. Ayrıca canlıda risk tavanı ayrıca kısılır.
    canli_onay: str = _env("CANLI_ONAY", "")
    # Canlıda işlem başına risk tavanı (kâğıttaki %2 canlı için fazla agresif).
    canli_risk_tavani: float = float(_env("CANLI_RISK_TAVANI", "0.01"))

    # ---- Vadeli işlem (futures_paper) ayarları ----
    # Kaldıraç neden 2? (2026-09-07 ölçümü — PORTFÖY motoru, backtest/portfoy.py)
    #
    # ÖNCEKİ KARAR GEÇERSİZ ÇIKTI. 2026-09-06'da ayar_etkisi.py ile 1x ve 2x
    # BİREBİR ÖZDEŞ ölçülmüş ve "2x bedelsiz risk" diye 1x'e inilmişti. O test
    # AYRI BAKİYELİYDİ (her parite kendi $10.000'i) — orada tek pozisyon marj
    # tavanına hiç değmez, dolayısıyla kaldıraç ölü değişkendi. Canlı bot ise
    # TEK bakiyeyi paylaşıyor ve aynı anda 4 pozisyon tutuyor; orada marj
    # gerçekten bağlayıcı. Ortak bakiyeli portföy motorunda yeniden ölçüldü:
    #
    #        Getiri   MaxDD   Sharpe  İşlem  Kazanma  Likid.
    #   1x   %94.7   -%27.8    0.86    1356   %39.1     0
    #   2x  %128.7   -%29.6    0.93    1356   %39.1     0     <- seçildi
    #   3x  %144.6   -%31.2    0.96    1356   %39.1     0
    #   5x  %149.4   -%33.3    0.95    1356   %39.1     8     <- duvar
    #
    # MEKANİZMA: işlem sayısı ve kazanma oranı değişmiyor (1356 / %39.1) —
    # aynı işlemler, farklı boyut. 1x'te açık pozisyon nakdin tamamını kilitler,
    # 2./3./4. pozisyon kalan cılız nakitten boyutlanır. $10k equity + 3 açık
    # pozisyon örneğinde 1x nakdi $5.500'e düşürür (4. işlem equity'nin %0.55'i
    # kadar risk alır), 2x'te marj yarıya inip nakit $7.750 olur (%0.78). Yani
    # 1x "daha az risk" değil, NİYET EDİLEN %1'in altında kalmak.
    # Üç kapı da geçildi: 1. yarı %5.8->%12.4, 2. yarı %93.0->%114.0, tam dönem.
    #
    # NEDEN 3x/5x DEĞİL: likidasyon tamponu = %90/kaldıraç (LIQ_BUFFER).
    # 1x->%90, 2x->%45, 3x->%30, 5x->%18. 3x'in %30 tamponu 1356 işlemde hiç
    # delinmedi; 2x onun 1.5 katı pay bırakıyor. 5x'te 8 likidasyon = sınır.
    # Kâğıt aşamasında marj bırakmayı tercih ediyoruz; canlı veriyle (Faz 1
    # kapısı, ~100 kapanmış işlem) 3x yeniden değerlendirilebilir.
    #
    # AÇIK POZİSYONLARA ETKİSİ YOK: leverage yalnızca _open() içinde okunur;
    # açık pozisyonlar kendi `margin` değerlerini kayıtta taşır.
    #
    # SHORT: eski 3-parite testinde 9/9 zarar etmişti, ama 10 paritelik yeni
    # ölçümde short tarafı KÂRLI ve getirinin ana kaynağı (sadece-long %4.8'e
    # karşı long+short %11.8). .env'de ALLOW_SHORT=true ile açık.
    leverage: float = float(_env("LEVERAGE", "1"))
    allow_long: bool = _env("ALLOW_LONG", "true").lower() == "true"
    allow_short: bool = _env("ALLOW_SHORT", "false").lower() == "true"
    futures_taker_fee: float = 0.0005          # USDT-M taker %0.05
    funding_daily_long: float = 0.0003         # long öder: ~%0.01/8s
    funding_daily_short: float = 0.00015       # muhafazakâr short varsayımı
    panel_port: int = int(_env("PANEL_PORT", "8484"))
    # Sunucuda çalışırken 0.0.0.0 gerekir (container dışından erişim için).
    # Yayınlanan portu MUTLAKA Tailscale IP'sine bağlayın — bkz. docker-compose.
    panel_host: str = _env("PANEL_HOST", "127.0.0.1")
    # Panelden güncelleme tetikleme ucu (/api/guncelle). Panel yalnızca Tailscale
    # arayüzünde dinlediği için ağ dışına kapalı, tetik de SABİT bir betiği
    # çalıştırır (parametre almaz). Yine de ağdaki herkese "dağıtımı başlat"
    # yetkisi verir; kapatmak için .env'e DEPLOY_ENDPOINT=false yaz.
    # Teşhis ucu (/api/deploy-durum) salt-okunurdur, bu bayrakla kapanmaz.
    deploy_endpoint: bool = _env("DEPLOY_ENDPOINT", "true").lower() == "true"
    symbols: tuple[str, ...] = tuple(
        s.strip().upper() for s in _env("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if s.strip()
    )
    timeframe: str = _env("TIMEFRAME", "4h")  # 1h backtestte komisyona yenildi; 4h sağlam

    # Risk yönetimi
    risk_pct: float = float(_env("RISK_PCT", "0.02"))                 # işlem başına risk: bakiyenin %2'si
    max_daily_loss_pct: float = float(_env("MAX_DAILY_LOSS_PCT", "0.05"))  # günlük devre kesici: %5
    max_balance_usage: float = 0.95    # bakiyenin en fazla %95'i tek pozisyona girebilir

    # PORTFÖY KORUMASI — eşzamanlı pozisyon tavanı (0 = sınırsız)
    # Neden: 10 paritenin ortalama ikili korelasyonu 0.681 ölçüldü. Bu yüzden
    # "10 ayrı pozisyonda %1 risk" aslında BAĞIMSIZ 10 bahis değil; formül
    #   gerçek risk = r×√(N + N(N−1)ρ) = %1×√(10+90×0.681) ≈ %8.4
    #   bağımsız bahis = N/(1+(N−1)ρ) = 1.4
    # yani tek yönde ~%8.4'lük TEK bir bahis. Portföy backtest'i (tek bakiye,
    # backtest/portfoy.py) bunu doğruladı: tavansız MaxDD %31.5 — oysa ayrı
    # bakiyeli eski ölçümler %12 gösteriyordu.
    # Ölçüm: tavan 4 ile MaxDD %31.5→%27.8, Sharpe 0.74→0.86, getiri %99.9→%94.7.
    # Bu bir GETİRİ optimizasyonu değil, kuyruk riski kontrolüdür.
    # NOT: YÖN tavanı (aynı yönde en fazla N) ayrıca test edildi ve ZARARLI
    # çıktı (Sharpe 0.86→0.57) — stratejinin kârı baskın yönde olmaktan geliyor.
    # Sayıyı sınırla, yönü değil.
    max_concurrent_positions: int = int(_env("MAX_CONCURRENT_POSITIONS", "4"))

    # KÂR BİLDİRİMİ — pozisyon N×R kâra ulaşınca Telegram'a haber ver (0 = kapalı).
    # KAPATMAZ. Otomatik kâr hedefi test edildi ve mevcut parite listesiyle
    # sağlamlık çıtasını geçemedi (ilk yarıda berabere), o yüzden karar
    # kullanıcıda: bildirim gelir, dilerse panelden KAPAT'a basar.
    # Zaten 3×ATR iz süren stop ile 4R'ye ulaşıldığında stop matematiksel olarak
    # giriş+3R'de kilitlidir (tepe−3ATR = giriş+12ATR−3ATR), yani kârın dörtte
    # üçü garanti altındadır — "stop'u yukarı çek" ihtiyacı otomatik karşılanır.
    # Varsayılan 4.0'dı; kullanıcı HER R'da haber istedi (2026-09-10) → 1.0.
    # Her tam R eşiği (1R, 2R, 3R...) BİR kez bildirilir; eşik etrafında
    # gidip gelme spam yapmaz (r_notified cırcırı yalnızca yukarı sayar).
    r_notify_level: float = float(_env("R_NOTIFY_LEVEL", "1.0"))

    # Komisyon optimizasyonu:
    # - Binance'te "BNB ile komisyon öde" açıksa spot ücret %0.10 → %0.075 düşer.
    #   (Bunu borsa arayüzünden açmanız ve az miktar BNB tutmanız gerekir.)
    # - use_limit_entry: girişte önce maker limit emri dener (taker yerine maker ücreti);
    #   dolmazsa timeout sonunda iptal edip market emrine döner.
    use_limit_entry: bool = _env("USE_LIMIT_ENTRY", "true").lower() == "true"
    limit_entry_timeout_s: int = int(_env("LIMIT_ENTRY_TIMEOUT_S", "45"))

    # API anahtarları
    testnet_key: str = _env("BINANCE_TESTNET_KEY")
    testnet_secret: str = _env("BINANCE_TESTNET_SECRET")
    live_key: str = _env("BINANCE_LIVE_KEY")
    live_secret: str = _env("BINANCE_LIVE_SECRET")

    # Telegram
    telegram_token: str = _env("TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = _env("TELEGRAM_CHAT_ID")
    # ÇİFT YÖNLÜ KOMUT (/durum, /kapat, /stop) — kapatmak için .env'e
    # TELEGRAM_KOMUT=false.
    # GÜVENLİK: bot kullanıcı adını bilen HERKES bota yazabilir. Komutu
    # çalıştırma yetkisi YALNIZCA TELEGRAM_CHAT_ID'dedir; başkasının mesajı
    # uygulanmaz, sahibe bir kez haber verilir. Yıkıcı komutlar (/kapat,
    # /stop) ayrıca 4 haneli, 2 dakika ömürlü onay kodu ister.
    # chat_id boşsa katman hiç açılmaz — beyaz liste yoksa kapı da yok.
    telegram_komut: bool = _env("TELEGRAM_KOMUT", "true").lower() == "true"

    # Zamanlama
    poll_seconds: int = 30             # ana döngü periyodu (stop takibi + mum kontrolü)
    daily_report_hour: int = int(_env("DAILY_REPORT_HOUR", "21"))

    # ---- BORSA kanalı (ABD + BIST, SANAL cüzdanla paper) ----
    # Kripto botundan tamamen ayrı: ayrı DB, ayrı cüzdanlar (USD + TRY), ayrı
    # iş parçacığı. Günlük mum + haftalık teyit; BIST long-only (borsa_trader.py).
    # Kapatmak için .env'e BORSA_ENABLED=false yaz.
    borsa_enabled: bool = _env("BORSA_ENABLED", "true").lower() == "true"
    borsa_symbols: tuple[str, ...] = tuple(
        s.strip().upper() for s in _env(
            "BORSA_SYMBOLS",
            "SPY,QQQ,NVDA,AAPL,MSFT,AMZN,META,GOOGL,TSLA,AMD,"
            "XU100.IS,THYAO.IS,ASELS.IS,GARAN.IS,AKBNK.IS,EREGL.IS,TUPRS.IS,"
            "SISE.IS,KCHOL.IS,BIMAS.IS").split(",") if s.strip()
    )
    borsa_poll_seconds: int = int(_env("BORSA_POLL_SECONDS", "300"))  # veri 15dk gecikmeli; sık sormak anlamsız
    borsa_risk_pct: float = float(_env("BORSA_RISK_PCT", "0.01"))     # işlem başına cüzdanın %1'i
    borsa_max_positions: int = int(_env("BORSA_MAX_POSITIONS", "3"))  # cüzdan başına eşzamanlı tavan
    borsa_db_path: Path = DATA_DIR / "borsa_state.db"

    # ---- SCALP kanalı (kısa süreli kaldıraçlı gir-çık) ----
    # Ana kanaldan TAM YALITIM: ayrı DB, ayrı cüzdan, ayrı istatistik, ayrı
    # iş parçacığı. İki parametre (Donchian + ATR) ve 15 dakikalık mum.
    # Kapatmak için .env'e SCALP_ENABLED=false.
    scalp_enabled: bool = _env("SCALP_ENABLED", "true").lower() == "true"
    # paper | testnet | live — KagitBroker mı FuturesBroker mı kullanılacağını
    # bu belirler. Trader hangisi olduğunu bilmez (bkz. kagit_broker.py).
    scalp_mode: str = _env("SCALP_MODE", "paper").lower()
    # PARİTE SAYISI, işlem sıklığının EN UCUZ kolu. 2026-10-02 ölçümü
    # (20 parite, 15dk, ~10 günlük veri): Donchian 20 → 145.8 sinyal/gün,
    # Donchian 10 → 234.8. Yani "daha çok işlem" için mum boyunu düşürmeye
    # hiç gerek yok — düşürmek sürtünmeyi hedefin önüne geçirirdi (1dk'da
    # %135). Parite eklemek işlem başına ekonomiyi HİÇ bozmaz; sadece aynı
    # kalitede daha çok fırsat tarar.
    scalp_symbols: tuple[str, ...] = tuple(
        s.strip().upper() for s in _env(
            "SCALP_SYMBOLS",
            "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,"
            "AVAXUSDT,LINKUSDT,DOTUSDT,LTCUSDT,ATOMUSDT,NEARUSDT,FILUSDT,"
            "INJUSDT,ARBUSDT,OPUSDT,APTUSDT,SUIUSDT,MATICUSDT"
        ).split(",") if s.strip())

    # ZAMAN DİLİMİ — 1 SAAT. 15dk ile başlandı ve ÖLÇÜMLE ÇÜRÜTÜLDÜ.
    #
    # 2026-10-02, 809 geçmiş işlem (19 parite, 15dk, Donchian 10, 1×ATR,
    # hedef 1.5R, taker): beklenti −0.724R/işlem. Canlı kâğıt da bunu
    # doğruladı (28 pozisyon, kazanma %14.8, günlük %3 kesici tetiklendi).
    # Yani şanssızlık değil, aritmetik: sürtünme işlem başına 0.39R yiyordu.
    #
    # Hangi kolun ne kadar işe yaradığı ayrı ayrı ölçüldü:
    #   15dk · 1×ATR · hedef 1.5R · taker  → −0.724R   (başlangıç)
    #   + maker ücret                      → −0.094R   (sürtünme 0.39→0.13R)
    #   + 1 saate çık                      → +0.196R   (kazanma %34→%41)
    #   + 3×ATR, dar hedefi kaldır         → +0.195R   (ödeme 1.56→2.30)
    # Doğrulama: simülasyon ANA KANALIN ayarını (4sa·3×ATR·hedefsiz)
    # +0.116R buluyor — ana kanal gerçekten kârlı, yani model güvenilir.
    #
    # Seçilen ayar üç sürtünme varsayımında da ARTI:
    #   maker+kaymasız %0.065 → +0.181R · maker+kayma %0.14 → +0.156R
    #   taker+kayma    %0.200 → +0.136R
    # Bu yüzden maker giriş YAZILMADI: gerek yok ve limit emrin dolum
    # belirsizliğini modellemek yeni bir sapma kaynağı olurdu.
    scalp_timeframe: str = _env("SCALP_TIMEFRAME", "1h")

    # İKİ PARAMETRE. Donchian = zamanlama, ATR çarpanı = stop mesafesi.
    # Ana kanalın 3.0'ı burada fazla geniş olurdu (hedef sürtünmeyi karşılamaz).
    # Donchian 10 = son 2.5 saatin kırılımı (20 → 5 saat). Ölçümde sinyali
    # %61 artırıyor ve maliyet kapısına takılma oranı DEĞİŞMİYOR (%18→%19) —
    # yani sinyal kalitesini bozmadan sıklık kazandırıyor. Ana kanal da
    # .env'de 10 kullanıyor, yani bu değer projede zaten sınanmış.
    scalp_donchian: int = int(_env("SCALP_DONCHIAN", "10"))
    # ATR ÇARPANI 3 — 1'den yükseltildi. 1×ATR stopu işlemlerin %75'ini
    # gürültüye kurban ediyordu (ölçüm: 15dk'da kazanma %25, 3×ATR'de %37).
    # Ana kanal da 3 kullanıyor; "dar stop daha az risk" sezgisi yanlış çıktı,
    # çünkü dar stop daha SIK tetiklenir ve her tetik sürtünme öder.
    scalp_atr_carpani: float = float(_env("SCALP_ATR_CARPANI", "3.0"))

    # ÇIKIŞ: hedefte YARISI kapanır, kalan iz süren stopla devam eder ve
    # stop en az girişe çekilir (koşan yarı zarar edemez).
    #
    # EŞİK 4R — 1.5R'den yükseltildi. Yarı kâr almak beklentiye MAL OLUYOR
    # (ölçüm, 1sa·3×ATR, 429 işlem):
    #   hedef yok    +0.195R      yarısı 2.5R'de  +0.163R  (−%16)
    #   yarısı 1.5R  +0.135R      yarısı 4.0R'de  +0.184R  (−%6)
    # Kullanıcı "belli kâr marjında çıkış" istedi; özellik korunuyor ama
    # eşiği bedeli en düşük yere çekildi. 1.5R'de kalmak beklentinin
    # üçte birini yiyordu — kâr alma hissinin bedeli bu kadar olmamalı.
    scalp_kar_hedefi_r: float = float(_env("SCALP_KAR_HEDEFI_R", "4.0"))

    # MALİYET KAPISI — bu kanalın yaşam savaşı. Hedef, gidiş-dönüş
    # sürtünmenin en az bu kadar katı olmalı; değilse işleme GİRİLMEZ.
    # Projenin kendi notu: "1h backtestte komisyona yenildi" — orada sürtünme
    # hedefin %14'üydü. Bu kapı aynı hatayı sessizce tekrarlamayı yasaklar.
    scalp_min_hedef_kat: float = float(_env("SCALP_MIN_HEDEF_KAT", "3.0"))
    scalp_taker_fee: float = float(_env("SCALP_TAKER_FEE", "0.0005"))
    scalp_slippage: float = float(_env("SCALP_SLIPPAGE", "0.0005"))

    # RİSK VE FRENLER. Scalp ana kanaldan çok daha sık işlem açar; işlem
    # başına risk bu yüzden daha küçük, frenler daha sıkı.
    # RİSK, İŞLEM SAYISIYLA TERS ORANTILI AYARLANIR — yoksa frenler her gün
    # kapanır. 2026-10-02 hesabı (kazanma %40 varsayımı, günlük %3 kesici):
    #   risk %0.50 →  6 üst üste zarar kesiciyi tetikler → 60 işlemde %100
    #   risk %0.25 → 12 üst üste zarar gerekir           → 60 işlemde %5
    # Yani "bol işlem" isteniyorsa risk yarıya inmek ZORUNDA; inmezse kanal
    # her gün kendi kesicisine çarpıp durur ve hiç işlem göremezsin.
    scalp_risk_pct: float = float(_env("SCALP_RISK_PCT", "0.0025"))      # %0.25
    scalp_kaldirac: float = float(_env("SCALP_KALDIRAC", "3"))
    # 6 pozisyon × %0.25, korelasyon 0.681 ile GERÇEK risk ~%1.29
    # (bağımsız sanılan %1.50 değil) — günlük %3 kesicinin rahat altında.
    scalp_max_pozisyon: int = int(_env("SCALP_MAX_POZISYON", "6"))
    # Dar stop + sabit %risk, farkında olmadan çok büyük notional üretir
    # (küçük bölen). 0 = otomatik: kaldıraç ve slot sayısından TÜRETİLİR
    # (scalp_notional_tavani). Sabit sayı yazmak tehlikeliydi — 2026-10-02'de
    # %150'de kalmıştı ve 6 slotla marj fiziken sığmıyordu.
    scalp_max_notional_pct: float = float(_env("SCALP_MAX_NOTIONAL_PCT", "0"))
    scalp_max_gunluk_zarar: float = float(_env("SCALP_MAX_GUNLUK_ZARAR", "0.03"))
    # 1 saatlik mumla ölçülen akış ~10 işlem/gün (19 parite). Tavan 25 ile
    # bağlayıcı değil ama kaçak bir döngüye karşı tampon kalıyor. 60'ta
    # bırakmak "koruma var" yanılsaması üretirdi — hiç devreye girmezdi.
    scalp_max_gunluk_islem: int = int(_env("SCALP_MAX_GUNLUK_ISLEM", "25"))
    # Üst üste N zarar = rejim değişmiş olabilir; devam etmek komisyon bağışı.
    # AMA eşik işlem sayısına göre ayarlanmalı. 60 işlem/günde beklenen tetik:
    #   fren  5 → günde 1.87 kez (kanal çoğu gün KAPALI kalır)
    #   fren 10 → günde 0.15 kez (gerçek rejim değişimini yakalar, gürültüyü değil)
    # 5'te bırakmak "bol işlem" isteğini sessizce iptal ederdi.
    scalp_max_zarar_serisi: int = int(_env("SCALP_MAX_ZARAR_SERISI", "10"))
    scalp_allow_short: bool = _env("SCALP_ALLOW_SHORT", "true").lower() == "true"
    scalp_poll_seconds: int = int(_env("SCALP_POLL_SECONDS", "20"))
    scalp_baslangic_usdt: float = float(_env("SCALP_BASLANGIC_USDT", "10000"))

    # CANLI ALT HESAP ANAHTARLARI — ana hesaptan AYRI.
    # Binance tek-yön modunda aynı sembolde iki pozisyon BİRLEŞİR; scalp ile
    # trend aynı hesapta çalışırsa birbirinin pozisyonunu bozar. Çözüm: ayrı
    # alt hesap, ayrı anahtar. Bu alanlar boşsa canlı scalp BAŞLAMAZ.
    scalp_live_key: str = _env("SCALP_BINANCE_KEY")
    scalp_live_secret: str = _env("SCALP_BINANCE_SECRET")
    scalp_db_path: Path = DATA_DIR / "scalp_state.db"

    # Dosyalar
    db_path: Path = DATA_DIR / "bot_state.db"
    log_path: Path = DATA_DIR / "bot.log"

    strategy: StrategyParams = field(default_factory=StrategyParams)

    @property
    def scalp_notional_tavani(self) -> float:
        """Pozisyon başına notional tavanı — varlığın katı olarak.

        2026-10-02'de ilk scalp işlemleri bunu açığa çıkardı: tavan sabit
        %150 yazılıydı ve kaldıraç 3 ile her pozisyon varlığın %50'sini marj
        olarak kilitliyordu. 6 slot istenince gereken marj %300 oldu, yani
        3. pozisyondan sonrası SESSİZCE açılamazdı — "6 eşzamanlı pozisyon"
        ayarı kâğıt üzerinde kalırdı.

        Formül: tüm slotlar dolduğunda marj varlığın en çok %80'i olsun.
            N × (notional / kaldıraç) ≤ 0.8  →  notional ≤ 0.8 × kaldıraç / N
        Türetilmiş olması önemli: kullanıcı kaldıracı ya da slot sayısını
        değiştirdiğinde tavan kendiliğinden uyar, elle güncellenmeyi beklemez.
        """
        if self.scalp_max_notional_pct > 0:
            return self.scalp_max_notional_pct      # elle geçersiz kılma
        n = max(self.scalp_max_pozisyon, 1)
        return 0.8 * max(self.scalp_kaldirac, 1.0) / n

    @property
    def scalp_strategy(self) -> StrategyParams:
        """Scalp'in kendi indikatör parametreleri.

        compute_indicators() StrategyParams bekliyor; ana kanalın nesnesini
        verirsek Donchian 20/4saat ve ATR×3 kullanılır — scalp'in istediği
        bu değil. Ayrı nesne, aynı hesap kodu: tek indikatör uygulaması
        kalsın ki iki kanal arasında sapma olmasın.
        """
        return replace(self.strategy,
                       donchian_period=self.scalp_donchian,
                       atr_multiplier=self.scalp_atr_carpani)

    def validate(self) -> None:
        gecerli = ("dry_run", "testnet", "live", "futures_paper",
                   "futures_testnet", "futures_live")
        if self.mode not in gecerli:
            raise ValueError(f"Geçersiz BOT_MODE: {self.mode}")
        if self.mode.startswith("futures") and not (1 <= self.leverage <= 5):
            raise ValueError("LEVERAGE 1-5 arasında olmalı — üstü backtest'te değer üretmedi, risk üretti")
        if self.mode == "testnet" and not (self.testnet_key and self.testnet_secret):
            raise ValueError("testnet modu için BINANCE_TESTNET_KEY/SECRET gerekli (testnet.binance.vision)")
        if self.mode == "futures_testnet" and not (self.testnet_key and self.testnet_secret):
            raise ValueError("futures_testnet için BINANCE_TESTNET_KEY/SECRET gerekli "
                             "(testnet.binancefuture.com)")
        if self.mode == "live" and not (self.live_key and self.live_secret):
            raise ValueError("live modu için BINANCE_LIVE_KEY/SECRET gerekli")
        if not (0 < self.risk_pct <= 0.05):
            raise ValueError("RISK_PCT 0 ile 0.05 (%5) arasında olmalı — daha yükseği kumardır")

        # ---------------- GERÇEK PARA KAPISI ----------------
        # Üç ayrı kilit: anahtar + bilinçli onay dizgisi + kısılmış risk tavanı.
        # Amaç, "yanlışlıkla canlıya geçmiş olma" ihtimalini sıfırlamaktır;
        # bu moda ancak isteyerek ve okuyarak girilebilir.
        if self.mode == "futures_live":
            if not (self.live_key and self.live_secret):
                raise ValueError("futures_live için BINANCE_LIVE_KEY/SECRET gerekli")
            if self.canli_onay != "EVET_GERCEK_PARA":
                raise ValueError(
                    "GERÇEK PARA MODU KİLİTLİ. Açmak için .env'e şunu ekle:\n"
                    "  CANLI_ONAY=EVET_GERCEK_PARA\n"
                    "Bu kasıtlı bir engeldir: canlıya geçiş kazara olmamalı. "
                    "Öncesinde `python -m src.canli_kontrol` çalıştırılmalıdır.")
            if self.risk_pct > self.canli_risk_tavani:
                raise ValueError(
                    f"Canlıda RISK_PCT ({self.risk_pct:.3f}) tavanı "
                    f"({self.canli_risk_tavani:.3f}) aşıyor. Kâğıttaki risk "
                    f"canlıya olduğu gibi taşınmaz; CANLI_RISK_TAVANI ile "
                    f"bilinçli olarak yükseltilebilir.")

        # ---------------- SCALP KANALI KAPILARI ----------------
        if self.scalp_mode not in ("paper", "testnet", "live"):
            raise ValueError(f"Geçersiz SCALP_MODE: {self.scalp_mode}")
        if self.scalp_mode == "live":
            # AYRI ALT HESAP ŞART. Binance tek-yön modunda aynı sembolde iki
            # pozisyon BİRLEŞİR: scalp, trend kanalının pozisyonunu bozar ve
            # bunu kimse fark etmez (iki ayrı DB kendi doğrusunu yazar).
            # Ana hesabın anahtarını buraya yazmak da bu yüzden yasak.
            if not (self.scalp_live_key and self.scalp_live_secret):
                raise ValueError(
                    "Canlı scalp için SCALP_BINANCE_KEY/SECRET gerekli — ve bu "
                    "anahtar AYRI BİR BİNANCE ALT HESABINA ait olmalı. Ana "
                    "hesapla paylaşılırsa trend pozisyonlarıyla birleşir.")
            if self.scalp_live_key == self.live_key:
                raise ValueError(
                    "SCALP_BINANCE_KEY, BINANCE_LIVE_KEY ile AYNI. Aynı hesapta "
                    "iki kanal çalıştırmak pozisyonları birleştirir; alt hesap aç.")
            if self.canli_onay != "EVET_GERCEK_PARA":
                raise ValueError(
                    "Canlı scalp KİLİTLİ. .env'e CANLI_ONAY=EVET_GERCEK_PARA ekle.")
        if self.scalp_min_hedef_kat < 1.0:
            raise ValueError(
                "SCALP_MIN_HEDEF_KAT 1'in altında olamaz: hedefin sürtünmeden "
                "küçük olmasına izin vermek, bilerek komisyon bağışlamaktır.")


CONFIG = Config()
