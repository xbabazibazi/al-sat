# Backtest Sonuçları

Tüm testler: gerçek Binance verisi, komisyon + kayma + funding dahil,
bar-içi stop simülasyonu, canlı botla aynı strateji modülü.

## Nihai konfigürasyon (spot, 4h, ATR 3.0, Donchian 20)

| Parite | Getiri (4 yıl) | CAGR | Sharpe | MaxDD | İşlem | Kazanma | PF |
|---|---|---|---|---|---|---|---|
| BTCUSDT | +%59.9 | %12.4 | 0.93 | −%16 | 104 | %40 | 1.62 |
| ETHUSDT | +%50.9 | %10.8 | 0.85 | −%12 | 105 | %37 | 1.59 |
| SOLUSDT | +%52.3 | %11.1 | 0.87 | −%18 | 101 | %40 | 1.56 |

## Walk-forward (12 ay eğit → 6 ay test, kaydırmalı)

| Parite | Bileşik OOS getiri | Pozitif pencere |
|---|---|---|
| BTC | +%33.4 | 5/6 |
| ETH | +%33.5 | 5/6 |
| SOL | +%43.2 | 4/6 |

18 pencerenin 14'ü pozitif. **En güvenilir kanıt bu tablodur.**

## Vadeli işlem matrisi (2x, long+short, analiz katmanlı, Donchian 10)

| Parite | Getiri | MaxDD | Sharpe | İşlem/ay | Long/Short |
|---|---|---|---|---|---|
| BTC | +%8.2 | −%29.8 | 0.21 | 3.5 | 104/65 |
| ETH | +%7.2 | −%26.1 | 0.20 | 3.7 | 92/87 |
| SOL | +%49.3 | −%15.0 | 0.80 | 3.5 | 76/94 |
| BNB | +%27.9 | −%21.3 | 0.50 | 3.6 | 101/71 |

Likidasyon: 0 (tüm konfigürasyonlarda).

## Elenen fikirler (test edildi, reddedildi)

### Kısa zaman dilimleri
| Dilim | İşlem/gün | Sonuç |
|---|---|---|
| 4h | 0.07 | **+%63** ✅ |
| 1h | 0.30 | −%43 |
| 15m | 1.34 | −%96 |
| 5m | 4.36 | −%99.8 💀 |

Sıklık arttıkça komisyon kârı yiyor, kazanma oranı %18'e düşüyor.

### Sezgi testi: "tepeden short, dipten long"
| Parite | Kırılımı takip (mevcut) | Tepeden short/dipten long |
|---|---|---|
| BTC | +%45.6 | −%40.1 |
| ETH | +%56.6 | −%53.2 |
| SOL | +%51.2 | −%32.3 |
| BNB | +%47.2 | −%66.2 |

Ortalamaya dönüş 4/4 paritede zarar etti. Trend takibi doğru yaklaşım.

### Her zaman piyasada (EMA kesişimi long/short)
BTC −%21.8, ETH −%17.5, SOL +%75.9 → tutarsız, kazanma oranı %25.

### Kaldıraç etkisi (spot varlık eğrisine uygulanmış)
| Kaldıraç | BTC getiri | MaxDD | Funding maliyeti (4 yıl) |
|---|---|---|---|
| Spot | +%60 | −%16 | $0 |
| 2x | +%49 | −%33 | sermayenin %89'u |
| 3x | +%63 | −%47 | %166 |
| 10x | −%56 | −%92 | %416 |

Kaldıraç getiriyi artırmıyor, riski ve funding maliyetini katlıyor.

### Short-only ve günlük filtre
- Short-only: 9/9 konfigürasyonda zarar (günlük ayı filtresiyle bile)
- Günlük EMA200 filtresi: SOL +10p, ETH −25p → tutarsız, varsayılan kapalı

### Elenen pariteler
XRP (−%17), LINK (−%8), AVAX (~0), ADA (+%3.7), DOGE (+%7.1) — zayıf/negatif.

## Komisyon optimizasyonu (kabul edildi)

| Senaryo | BTC getiri |
|---|---|
| Temel (%0.10) | +%53.6 |
| + BNB indirimi (%0.075) | +%39.9* |
| + Maker limit girişi | +%41.1* |

*Günlük filtre açıkken ölçüldü; komisyon katkısı üç paritede de +2-4 puan.
