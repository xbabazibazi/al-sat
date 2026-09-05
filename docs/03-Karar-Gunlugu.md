# Karar Günlüğü

Projedeki her önemli karar, gerekçesi ve dayandığı ölçüm. Kural:
**test edilmeyen fikir koda girmez.**

## 2026-09-05

**Proje başladı** — Bir Gemini sohbetindeki bot rehberi incelendi. Rehberin
mimarisi mantıklıydı ama kodunda 8 kritik açık vardı: RAM'de pozisyon durumu,
bot içinde stop (borsada değil), Yahoo verisiyle backtest / Binance ile canlı,
aynı mumda tekrar alım döngüsü, `round()` ile lot yuvarlama, bakiyeyi aşabilen
risk hesabı, testnet verisiyle sinyal, zayıf giriş sinyali. Hepsi baştan çözüldü.

**4 saatlik dilim seçildi** — 1h dilim tüm parametre kombinasyonlarında zarar
etti (komisyon kârı yiyor). 4h + ATR 3.0 üç paritede de kârlı.

**Komisyon optimizasyonu kabul** — BNB indirimi + maker limit girişi üç
paritede de +2-4 puan. Varsayılan açık.

**Günlük EMA200 filtresi reddedildi** — A/B testinde tutarsız (SOL +10p,
ETH −25p). Kod duruyor, varsayılan kapalı (`USE_DAILY_FILTER=false`).

**Walk-forward eklendi** — Tek bölmeli optimizasyon yerine kayan pencere.
Kâr üretmez ama yanlış kârı canlıya taşımayı engeller.

**Kaldıraç incelendi** — Kullanıcı kaldıraçlı işlem istedi. Ölçüm: kaldıraç
pozisyonu büyütmüyor (boyutu %2 risk kuralı belirliyor), 2x ideal, 3x+ katkısız,
10x hesabı sıfırlıyor. `LEVERAGE=2`, config 5 üstünü reddediyor.

**Short varsayılan kapalı, sonra kullanıcı talebiyle açıldı** — Backtest'te
short 9/9 konfigürasyonda zarar etti. Kullanıcı long+short istedi; paper modda
risksiz gözlemlenmek üzere açıldı (`ALLOW_SHORT=true`).

**Ön değerlendirme katmanı eklendi** — Kullanıcı talebi: "inceleme olmadan
hiçbir işleme girme". 5 araçlı skor sistemi, VETO mekanizmalı, her karar
veritabanına yazılıyor ve panelde görünüyor.

**Panel kuruldu** — localhost:8484. Anlık pozisyon PnL, varlık eğrisi, işlem
geçmişi, ön değerlendirme kartı, izleme listesi (tetiğe uzaklık).

**Aktif mod (Donchian 10)** — Kullanıcı daha sık işlem istedi. Kısa dilimler
test edildi ve elendi (5m: −%99.8). Kırılım penceresini daraltmak ise 4
paritede de pozitif kaldı: ayda ~14 işlem. Uygulandı.

**BNB parite listesine eklendi** — 6 aday tarandı, yalnızca BNB pozitif (+%18).
XRP, LINK, AVAX, ADA, DOGE elendi.

**Günlük sermaye stopu genişletildi** — Kullanıcı talebi: günlük yüzde
düşüşte stop. Devre kesici artık sadece yeni girişleri değil, açık
pozisyonları da kapatıyor (`MAX_DAILY_LOSS_PCT=0.05`).

## 2026-09-06

**"Tepeden short, dipten long" sezgisi test edildi ve reddedildi** —
Kullanıcı panelde tersini gördüğünü belirtti. Ortalamaya dönüş varyantı 4/4
paritede zarar etti (BTC −%40, ETH −%53, SOL −%32, BNB −%66). Trend takibi
korundu, dokümante edildi.

**BAŞLAT/DURDUR butonları + watchdog** — Kullanıcı talebi: "başlat deyince bir
daha kapanmasın". Panel botu bağımsız süreç olarak başlatıyor, kalp atışı
(30 sn) izleniyor, 90 sn atış gelmezse otomatik yeniden başlatılıyor.
Test edildi: bot zorla öldürüldü, 45 saniyede geri geldi.

**Çifte çalışma kilidi** — İki bot aynı anda çalışırken yakalandı (çifte
pozisyon riski). Artık taze kalp atışı varsa ikinci bot başlamayı reddediyor.

**Wiki/dokümantasyon** — `docs/` klasörü oluşturuldu; GitHub ve Gitea
wiki'lerinde de yayınlanabilir.
