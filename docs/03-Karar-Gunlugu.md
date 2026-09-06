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

## 2026-09-06 (ikinci oturum) — kâr realizasyonu araştırması

Kullanıcı sorusu: "kârı nasıl realize edelim?" İki ayrı şey ölçüldü:
işlem seviyesinde çıkış şeması, ve ayarların getiriye etkisi.

**Önce bir düzeltme:** önceki oturumda "kısmi çıkış getiriyi düşürür" denmişti
ama bu ÖLÇÜLMEMİŞ bir varsayımdı. Ölçüldü; yönü doğruymuş ama gerekçe eksikti.

### Ölçüm altyapısı
- `backtest/kar_cikis.py` — 10 çıkış şemasını aynı varsayımlarla karşılaştırır.
  Bar içi sıralama KARAMSAR: aynı barda stop ve kâr hedefi mümkünse stop
  önce sayılır, böylece kâr hedefi şemaları kayrılmaz.
  Motor `futures_engine` ile birebir doğrulandı (3 paritede $0.0000 fark).
- `backtest/ayar_etkisi.py` — canlı ayardan tek tek sapma etkisi.
- `backtest/kar_hedefi_saglamlik.py`, `backtest/esik_saglamlik.py` — üç
  kapılı sağlamlık çıtası: geniş plato + ≥7/10 parite + her iki dönemde üstünlük.

### Karar 1: Kâr hedefi EKLENMEDİ (sağlamlık testinden geçemedi)
10 parite ortalaması sert kâr hedefini cazip gösterdi (2R: +3.8pp, 5R: +1.1pp)
ve plato testini geçti (2R–8R arası hepsi bazı yener). Ancak:
- Parite kırılımı: 5R yalnızca 5/10 paritede kazandı; BTC ve ETH kötüleşti.
- Dönem testi: 5R ilk yarıda 3/10 (3.5% vs baz 3.8%) — üstünlük yok.
Üç kapıdan ikisinde çakıldı → REDDEDİLDİ. Saf iz süren stop korundu.
İz süren stop 10/10 paritede pozitif; bulunan alternatiflerin hiçbiri bu
tutarlılığı sağlamadı.

Kısmi çıkışlar da net zararlı: %50@1R getiriyi %11.8'den %7.9'a düşürdü.
Erken kâr alma, tüm kaybedenleri finanse eden büyük kazananları kesiyor.

### Karar 2: ANALYSIS_THRESHOLD 50 -> 20 (üç testi de geçti)
**Mekanizma:** giriş sinyali zaten fiyatın EMA200'ün doğru tarafında olmasını
şart koşuyor, dolayısıyla "4s Trend" oyu LONG'da her zaman +30. Eşik 50 iken
diğer araçlardan +20 daha gerekir; günlük trend aşağıysa (-20) kalan iki aracın
maksimumu +35 olduğundan toplam asla +20'ye ulaşamaz. Yani **eşik 50, günlük
trend aşağıyken LONG'u tamamen yasaklar** — bu, A/B testinde tutarsız bulunup
`USE_DAILY_FILTER=false` ile kapatılan günlük EMA filtresinin ta kendisidir.
Analiz katmanı onu arka kapıdan geri sokmuş.

**Falsifiye edilebilir kanıt** (`esik_saglamlik.py`):
| Eşik | Günlük filtre KAPALI | AÇIK | Fark | Değişmeyen parite |
|---|---|---|---|---|
| 50 | %11.8 | %11.8 | 0.00pp | 10/10 |
| 20 | %19.8 | %11.8 | −8.03pp | 0/10 |
| 0  | %20.5 | %11.8 | −8.71pp | 0/10 |
Eşik 50'de günlük filtreyi açmak hiçbir şeyi değiştirmiyor — çünkü zaten
uygulanıyor. İddia doğrulandı.

**Sağlamlık (10 parite, 4h, 2022-2026, 2x, risk %1):**
- Plato: eşik 0/10/20/25 hepsi ~%19.8–20.5; uçurum 30'da. Tek nokta değil.
- Parite: eşik 20, 9/10 paritede eşik 50'yi yendi (ETH +25pp, BNB +18pp;
  yalnızca DOT −4.5pp).
- Dönem: ilk yarı %3.8→%7.5, ikinci yarı %9.1→%13.6. İkisinde de üstün.

Bedeli: MaxDD %10.6 → %12.0 (1.4pp kötüleşme), işlem/yıl 41 → 60.
Getiri neredeyse ikiye katlanırken Sharpe 0.45 → 0.58 yükseliyor; takas iyi.

Plato ortası seçildi (kenar değil, eğri uydurmaya karşı): **20**.
Analiz katmanı KALDIRILMADI — kullanıcı şartı ("inceleme olmadan hiçbir işleme
girme") korunuyor: volatilite vetosu ve karşı-yön reddi aynen çalışıyor.

### Yan bulgu: kaldıraç 2x, 1x ile BİREBİR AYNI sonucu veriyor
Donchian 20'de 1x ve 2x satırları özdeş (%10.8, Sharpe 0.46, 31.4 işlem/yıl).
Pozisyon boyutunu risk belirlediği için kaldıraç yalnızca bakiye tavanını
gevşetiyor; o tavana hiç değilmiyor. Yani 2x, karşılığında hiçbir getiri
vermeden likidasyon riski taşıyor. Backtest'te 0 likidasyon görüldü, ama
1x'e inmek bedelsiz bir güvenlik kazancı olurdu — kullanıcı onayına bırakıldı.
