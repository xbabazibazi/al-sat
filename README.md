# Binance Trend-Takip Botu

EMA200 trend filtresi + Donchian kırılımı ile giriş yapan, ATR tabanlı izleyen
stop ile çıkan, risk-yönetimli Binance spot botu.

## Gemini sohbetindeki örneklerden farkı (neden bu yapı güvenli)

| Sorun (sohbetteki kod) | Bu projedeki çözüm |
|---|---|
| Pozisyon durumu RAM'de; bot çökünce unutuluyor (çifte alım riski) | SQLite kalıcı durum + açılışta borsayla **mutabakat** |
| Stop botun içinde; bot/PC çökerse pozisyon korumasız | **Borsa tarafında gerçek STOP_LOSS_LIMIT emri** |
| Backtest Yahoo verisiyle, canlı Binance verisiyle | Tek veri kaynağı: **Binance prod API** (backtest + canlı) |
| Backtest ve canlı bot farklı sinyal kodu çalıştırıyor | **Tek strateji modülü** (`src/strategy.py`) ikisinde de kullanılır |
| Aynı mumda satıp anında geri alma döngüsü | **Mum başına tek karar** (işlenen mum SQLite'ta) |
| `round()` ile lot yuvarlama → borsa emri reddi | `stepSize`'a **aşağı** yuvarlama + minQty/minNotional kontrolü |
| Risk hesabı bakiyeyi aşabiliyor | Boyut = min(risk sınırı, bakiyenin %95'i) |
| Testnet'in yapay fiyat verisiyle sinyal üretimi | Veri her modda prod'dan; **yalnızca emirler** testnet'e gider |
| Giriş sinyali "EMA üstü + RSI<50" (sürekli doğru olan durum) | Donchian **kırılım tetikleyicisi** (tek seferlik olay) |
| Günlük zarar limiti yok | **Devre kesici**: günlük %5 zararda yeni girişler durur |

## Kurulum

```bash
pip install -r requirements.txt
copy .env.example .env        # sonra .env içini doldurun
```

## Kullanım sırası (önerilen yol)

### 1. Backtest — strateji gerçekten kazanıyor mu?

```bash
# Tek backtest
python -m backtest.run --symbol BTCUSDT --start 2022-01-01 --end 2026-01-01

# İyileştirmelerin katkısını gösteren A/B tablosu
python -m backtest.run --symbol BTCUSDT --start 2022-01-01 --end 2026-01-01 --compare

# Parametre taraması + out-of-sample doğrulama (%70 eğitim / %30 test)
python -m backtest.run --symbol BTCUSDT --start 2022-01-01 --end 2026-01-01 --optimize

# Walk-forward: 12 ay eğit → 6 ay test, kaydırarak (EN güvenilir doğrulama)
python -m backtest.run --symbol BTCUSDT --start 2022-01-01 --end 2026-01-01 --walkforward
```

Test diliminde zarar eden parametreyi canlıya almayın — overfitting'dir.
Aynı taramayı ETHUSDT ve SOLUSDT için de çalıştırıp tutarlılığa bakın.

**Doğrulanmış bulgular (2022–2026, 4h):**

- Walk-forward bileşik OOS getiri: BTC **+%33**, ETH **+%34**, SOL **+%43**
  (18 pencerenin 14'ü pozitif). Parametreler ATR×2.5–3.5 / Donchian 10–40
  bölgesinde kümeleniyor.
- Komisyon iyileştirmeleri (BNB indirimi + maker giriş) üç paritede de
  tutarlı +2 ilâ +4 puan katkı sağladı → varsayılan AÇIK.
- Günlük EMA200 trend filtresi TUTARSIZ çıktı (SOL'da +10 puan, ETH'de
  −25 puan) → varsayılan KAPALI (`USE_DAILY_FILTER=true` ile denenebilir).
- 1 saatlik dilim her kombinasyonda komisyona yenildi → 4h kullanın.

### 2. Dry-run — API anahtarı olmadan canlı sinyal takibi

`.env` → `BOT_MODE=dry_run` (varsayılan). Gerçek fiyatlarla sinyal üretir,
sanal 10.000 USDT ile emirleri simüle eder.

```bash
python -m src.main --once   # tek tur test
python -m src.main          # sürekli
```

### 3. Testnet — sahte parayla gerçek emir akışı

1. https://testnet.binance.vision → GitHub ile giriş → API Key oluştur
2. `.env` → `BOT_MODE=testnet` + anahtarları yaz
3. `python -m src.main`

En az 2-4 hafta testnet'te çalıştırıp Telegram loglarını backtest
beklentileriyle karşılaştırın.

### 4. Docker ile 7/24

```bash
docker compose up -d --build
docker logs -f binance_trading_bot
```

`./data` klasörü konteynere bağlanır: bot yeniden başlasa da pozisyon durumu
ve işlem geçmişi kaybolmaz.

### 5. Canlıya geçiş (acele etmeyin)

- Binance API anahtarında **Enable Withdrawals KAPALI** olmalı; sadece
  "Enable Reading" + "Enable Spot Trading".
- Binance hesap ayarlarından **"BNB ile komisyon öde"** seçeneğini açın ve
  cüzdanda az miktar BNB tutun — komisyon %0.10 → %0.075 düşer; backtest'te
  bunun tek başına +2-3 puan katkısı doğrulandı.
- Sabit IP'niz yoksa (CGNAT) IP whitelist kullanamazsınız — bu durumda
  anahtar güvenliği tamamen "çekim izni kapalı" + `.env` gizliliğine dayanır.
- `.env` → `BOT_MODE=live`, küçük bir bakiyeyle başlayın.

## Telegram bildirimleri

@BotFather'dan bot oluşturup token'ı, @userinfobot'tan chat id'nizi alın,
`.env` dosyasına yazın. Alım/satım, hata, devre kesici ve günlük özet
(varsayılan 21:00) bildirimleri gelir.

## Mimari

```
src/config.py    → tüm ayarlar (.env + varsayılanlar)
src/strategy.py  → indikatörler + sinyal kuralları (backtest ve canlı ortak!)
src/exchange.py  → veri (prod) + emir (dry_run/testnet/live) katmanı
src/state.py     → SQLite kalıcı durum (pozisyon, stop, mum, işlem geçmişi)
src/risk.py      → pozisyon boyutlandırma + günlük devre kesici
src/trader.py    → sembol başına işlem motoru + mutabakat + günlük rapor
src/main.py      → giriş noktası
backtest/        → veri indirici + canlı botla birebir aynı mantıklı motor
```

## Bilinen sınırlar / dürüst notlar

- Strateji uzun süreli yatay/ayı piyasada işlem açmaz (EMA200 filtresi) —
  bu bir hata değil, tasarım: nakitte bekler.
- Testnet fiyat defteri gerçek piyasadan sapabilir; testnet'teki dolum
  fiyatları canlıdan farklı olabilir.
- Backtest %0.1 komisyon + %0.05 kayma varsayar; gerçek kayma yüksek
  volatilitede daha kötü olabilir.
- Hiçbir backtest sonucu gelecekteki kârın garantisi değildir. Botun
  kaybetmeyi göze alamayacağınız parayla çalıştırılmaması gerekir.
