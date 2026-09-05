# Çalıştırma ve Bakım

## Günlük kullanım

1. `BASLAT.bat` çift tıkla (veya `python -m src.panel`)
2. Tarayıcıda **http://localhost:8484**
3. **▶ BAŞLAT** butonuna bas — bot çalışır ve düşerse otomatik geri gelir
4. Durdurmak için **■ DURDUR**

Panel kapansa bile bot çalışmaya devam eder (bağımsız süreç). Watchdog yalnızca
panel açıkken görev yapar; 7/24 garanti için paneli açık bırak veya Docker kullan.

## Panelde ne görünür

| Bölüm | İçerik |
|---|---|
| Üst şeritler | Bot durumu, mod, kaldıraç, günlük stop bilgisi |
| Özet kutuları | Toplam varlık, serbest bakiye, bugünkü PnL, işlem sayısı, kümülatif |
| Ön değerlendirme | Her paritenin kararı, skoru, araç oyları |
| Açık pozisyonlar | Giriş, anlık fiyat, stop, marjin, funding, **anlık kâr/zarar** |
| İzleme listesi | Her paritenin long/short tetiğine uzaklığı |
| Varlık eğrisi | Zaman içinde toplam varlık |
| Son işlemler | Kapanan işlemler, net PnL, çıkış nedeni |

## Telegram

`.env` içinde `TELEGRAM_BOT_TOKEN` ve `TELEGRAM_CHAT_ID` dolu olmalı.
Bildirimler: pozisyon açılış/kapanış, hata, günlük sermaye stopu, günlük özet
(varsayılan 21:00).

## Modlar

| Mod | Açıklama | Gerçek para |
|---|---|---|
| `futures_paper` | Vadeli simülasyon (kaldıraç, long/short) | ❌ Hayır |
| `dry_run` | Spot simülasyon | ❌ Hayır |
| `testnet` | Binance Spot Testnet | ❌ Hayır |
| `live` | Gerçek hesap | ✅ **EVET** |

## Bakım komutları

```bash
# Backtest — strateji hâlâ çalışıyor mu?
python -m backtest.run --symbol BTCUSDT --interval 4h --start 2022-01-01 --end 2026-01-01 --walkforward

# Vadeli matris (yön × kaldıraç)
python -m backtest.futures_run --all

# Durumu sıfırla (dikkat: işlem geçmişi silinir)
del data\bot_state.db
```

## Sorun giderme

| Belirti | Sebep / çözüm |
|---|---|
| Bildirim gelmiyor | Bot işlem açmamış olabilir — panelde izleme listesine bak |
| Bot başlamıyor | Başka bot çalışıyor olabilir (çifte çalışma kilidi) — DURDUR'a basıp tekrar dene |
| Panel açılmıyor | Port çakışması — `.env` içinde `PANEL_PORT` değiştir |
| İşlem açılmıyor | Normal: kırılım + analiz onayı birlikte gerekir. 4h dilimde ayda ~14 işlem beklenir |

## Yedekleme

Önemli dosyalar: `.env` (gizli anahtarlar — asla paylaşma), `data/bot_state.db`
(işlem geçmişi ve durum). `.gitignore` ikisini de repo dışında tutar.
