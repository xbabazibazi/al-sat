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

---

## GÜNCEL CANLI KONFİGÜRASYON (2026-09-06 sonrası)

En üstteki tablo **spot / Donchian 20 / risk %2** ile üretilmişti ve canlı botla
karşılaştırılamaz. Canlı bot vadeli, long+short, analiz katmanlı çalışıyor.
Güncel ayarın 10 paritedeki gerçek ölçümü:

**Ayar:** 4h · Donchian 10 · ATR×3.0 · kaldıraç 1x · risk %1 · long+short ·
analiz eşiği 20 · günlük sermaye stopu %10 (seçici)

> Bu tablo AYRI BAKİYELİ ölçümdür ve kaldıraç 1x ile üretilmiştir. Canlı kaldıraç
> 2026-09-07'de 2x yapıldı ama bu tablo DEĞİŞMEZ: ayrı bakiyede tek pozisyon marj
> tavanına hiç değmez, 1x ve 2x özdeş çıkar. Kaldıracın gerçek etkisi yalnızca
> ORTAK bakiyede görünür — aşağıdaki portföy bölümüne bakın.

| Parite | Getiri (4 yıl) | MaxDD | Sharpe | İşlem | Kazanma | PF | Likid. |
|---|---|---|---|---|---|---|---|
| BTCUSDT | +%6.6 | −%17.0 | 0.22 | 263 | %35.4 | 1.06 | 0 |
| ETHUSDT | +%30.0 | −%12.2 | 0.79 | 261 | %39.5 | 1.30 | 0 |
| SOLUSDT | +%25.9 | −%9.8 | 0.71 | 252 | %38.9 | 1.26 | 0 |
| BNBUSDT | +%32.6 | −%9.1 | 0.83 | 245 | %39.2 | 1.37 | 0 |
| DOTUSDT | +%10.5 | −%12.6 | 0.35 | 251 | %40.6 | 1.12 | 0 |
| FILUSDT | +%19.1 | −%13.7 | 0.53 | 252 | %38.9 | 1.19 | 0 |
| DOGEUSDT | +%22.6 | −%14.2 | 0.61 | 260 | %39.6 | 1.25 | 0 |
| INJUSDT | +%19.3 | −%8.3 | 0.57 | 232 | %39.7 | 1.24 | 0 |
| ARBUSDT | +%17.4 | −%14.9 | 0.72 | 180 | %41.7 | 1.29 | 0 |
| OPUSDT | +%14.2 | −%8.7 | 0.50 | 223 | %39.0 | 1.18 | 0 |
| **ORTALAMA** | **+%19.8** | **−%12.1** | **0.58** | 242 | **%39.3** | **1.23** | **0** |

10/10 parite pozitif. Likidasyon yok.

**Canlı takip için beklenen bant** (Faz 1 kapısı — bkz. yol haritası):
- Kazanma oranı: %32–46 (ölçüm %39.3)
- Ortalama kazanç ÷ ortalama kayıp: ≥ 1.8
- En derin düşüş: ≤ %20 (ölçüm %12.1; iki katı aşılırsa varsayım bozulmuştur)
- Likidasyon 0 · stopsuz pozisyon 0

**UYARI — backtest her pariteyi AYRI $10.000 ile çalıştırır.** Canlı bot 10
pariteyi TEK bakiyeyle paylaşır. Getiriler paritelerin ortalamasına yakın
seyretmeli ama birebir aynı olmaz. Ayrıca geçmiş performans gelecek getiriyi
garanti etmez; bu tablo bir beklenti bandıdır, taahhüt değil.

### Reddedilen alternatifler (kâr realizasyonu araştırması)

| Şema | Ort. getiri | Neden reddedildi |
|---|---|---|
| Kısmi çıkış %50 @1R | %7.9 | Net zararlı — büyük kazananları kesiyor |
| Kısmi çıkış %50 @2R | %9.7 | Net zararlı |
| Sert kâr hedefi @5R | %12.9 | Parite (5/10) ve dönem (3/10) testlerinde çakıldı |
| Sert kâr hedefi @3R | %13.5 | Yalnızca 8/10 pozitif; dönem tutarlılığı zayıf |
| 2R sonrası stop sıkılaştırma | %10.1 | Baz senaryodan kötü |
| **Saf iz süren stop (korundu)** | **%11.8** \* | 10/10 pozitif — tek tutarlı şema |

\* Bu sütun eski eşik 50 ile ölçülmüştür; şemalar arası karşılaştırma aynı
zeminde kalsın diye. Eşik 20 ile aynı şema %19.8 verir.

---

## ORTAK BAKİYE (portföy) ölçümü — `backtest/portfoy.py`

Yukarıdaki tabloların hepsi her pariteye AYRI $10.000 verir. Canlı bot tek
bakiyeyi paylaştığı için korelasyon, sermaye rekabeti ve portföy geneli devre
kesici oradaki sayılarda GÖRÜNMEZ. Ortak bakiyeli motor bu üçünü de modeller.

10 paritenin ortalama ikili 4h korelasyonu **0.681**. Bu yüzden "10 pozisyonda
%1 risk" bağımsız 10 bahis değil:
- gerçek risk = r×√(N + N(N−1)ρ) = %1×√(10+90×0.681) ≈ **%8.4**
- bağımsız bahis sayısı = N/(1+(N−1)ρ) ≈ **1.4**

Sonuç: tavansız gerçek MaxDD **−%31.5** — ayrı bakiyeli tablo −%12.1 gösteriyordu.
`MAX_CONCURRENT_POSITIONS=4` ile −%27.8'e iniyor (Sharpe 0.74→0.86, getiri
%99.9→%94.7). Bu bir getiri optimizasyonu değil, kuyruk riski kontrolüdür.

### Kaldıraç — ortak bakiyede (2026-09-07, tavan 4, risk %1, eşik 20)

| Kaldıraç | Getiri | MaxDD | Sharpe | İşlem | Kazanma | PF | Likidasyon | Liq. tamponu |
|---|---|---|---|---|---|---|---|---|
| 1x | %94.7 | −%27.8 | 0.86 | 1356 | %39.1 | 1.23 | 0 | %90 |
| **2x (canlı)** | **%128.7** | **−%29.6** | **0.93** | 1356 | %39.1 | 1.25 | 0 | %45 |
| 3x | %144.6 | −%31.2 | 0.96 | 1356 | %39.1 | 1.26 | 0 | %30 |
| 5x | %149.4 | −%33.3 | 0.95 | 1356 | %39.1 | 1.26 | **8** | %18 |

İşlem sayısı ve kazanma oranı tüm satırlarda AYNI (1356 / %39.1) — aynı işlemler,
farklı boyut. Kaldıraç marjı serbest bırakıp sonraki pozisyonların niyet edilen
%1 riske yaklaşmasını sağlıyor; 1x sistematik olarak 2./3./4. işlemi küçültüyordu.

Dönem ayrımı (üç kapı): 1. yarı %5.8→%12.4, 2. yarı %93.0→%114.0, tam dönem
%94.7→%128.7. Her üçünde de 2x > 1x.

Gerekçe ve 3x/5x'in neden seçilmediği: `src/config.py` içindeki `leverage` notu.
