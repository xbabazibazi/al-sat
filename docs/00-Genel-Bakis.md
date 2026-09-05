# AL-SAT Bot — Genel Bakış

Binance vadeli işlem (USDT-M) trend-takip botu. Long + short, kaldıraçlı,
ön değerlendirme katmanlı, kalıcı durumlu.

## Hızlı başlangıç

```bash
python -m src.panel          # panel: http://localhost:8484
# Panelde ▶ BAŞLAT butonuna bas — bot başlar ve düşerse otomatik geri gelir
```

veya `BASLAT.bat` dosyasına çift tıkla (bot + panel + tarayıcı).

## Mimari

| Dosya | Görevi |
|---|---|
| `src/config.py` | Tüm ayarlar (.env + varsayılanlar) |
| `src/strategy.py` | İndikatörler + giriş kuralları (long & short) |
| `src/analysis.py` | 5 araçlı ön değerlendirme — incelemesiz giriş yok |
| `src/futures_trader.py` | Vadeli paper motoru: marjin, funding, stop |
| `src/trader.py` | Spot motoru (borsa tarafında STOP_LOSS_LIMIT emri) |
| `src/exchange.py` | Veri (prod API) + emir katmanı |
| `src/state.py` | SQLite kalıcı durum (pozisyon, işlem, analiz, varlık) |
| `src/risk.py` | Pozisyon boyutlandırma + günlük devre kesici |
| `src/panel.py` | İzleme paneli + BAŞLAT/DURDUR + watchdog |
| `backtest/` | Backtest motorları (spot + vadeli), veri indirici |

## Güvenlik katmanları

1. **Her pozisyonda zorunlu stop** — ATR×3.0 mesafesinde, stop'suz pozisyon açılamaz
2. **İzleyen stop** — kâr yönünde ilerler, asla geri gitmez
3. **Günlük sermaye stopu** — gün içi %5 zararda tüm pozisyonlar kapanır, o gün giriş yok
4. **Pozisyon başı risk** — sermayenin %2'si
5. **Ön değerlendirme kapısı** — 5 araç onaylamadan giriş yok
6. **Çifte çalışma kilidi** — iki bot aynı anda çalışamaz
7. **Kalıcı durum** — çökme/yeniden başlatma pozisyonu unutturmaz

## İlgili sayfalar

- [Strateji Kuralları](01-Strateji.md)
- [Backtest Sonuçları](02-Backtest-Sonuclari.md)
- [Karar Günlüğü](03-Karar-Gunlugu.md)
- [Çalıştırma ve Bakım](04-Calistirma.md)
