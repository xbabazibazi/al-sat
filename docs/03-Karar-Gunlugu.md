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

---

## 2026-09-07 — Kaldıraç 1x → 2x (önceki kararın ÖLÇÜMÜ GEÇERSİZ ÇIKTI)

**Bir gün önceki "1x ve 2x birebir aynı" bulgusu yanlış genellemeydi.** O ölçüm
`ayar_etkisi.py` ile AYRI BAKİYELİ yapılmıştı: her parite kendi $10.000'i ile
çalışıyor, tek pozisyon marj tavanına asla değmiyor, dolayısıyla kaldıraç ölü
bir değişken. Canlı bot ise TEK bakiyeyi paylaşıyor ve `MAX_CONCURRENT_POSITIONS=4`
ile aynı anda dört pozisyon tutuyor — orada marj gerçekten bağlayıcı.

Ortak bakiyeli portföy motorunda (`backtest/portfoy.py`) yeniden ölçüldü
(10 parite, 4h, 2022-2026, Donchian 10, ATR×3.0, risk %1, eşik 20, tavan 4):

| Kaldıraç | Getiri | MaxDD | Sharpe | İşlem | Kazanma | PF | Likidasyon |
|---|---|---|---|---|---|---|---|
| 1x | %94.7 | −%27.8 | 0.86 | 1356 | %39.1 | 1.23 | 0 |
| **2x** | **%128.7** | **−%29.6** | **0.93** | 1356 | %39.1 | 1.25 | 0 |
| 3x | %144.6 | −%31.2 | 0.96 | 1356 | %39.1 | 1.26 | 0 |
| 5x | %149.4 | −%33.3 | 0.95 | 1356 | %39.1 | 1.26 | **8** |

**Mekanizma (getiri artışı nereden geliyor):** İşlem sayısı ve kazanma oranı
TÜM kaldıraçlarda birebir aynı (1356 / %39.1). Yani yeni bir strateji değil —
aynı işlemler, farklı boyut. 1x'te açılan pozisyon nakdin tamamını kilitler;
2., 3. ve 4. pozisyon kalan cılız nakitten boyutlanır. $10k equity + 3 açık
pozisyon örneğinde: 1x nakdi $5.500'e düşürür → 4. işlem equity'nin %0.55'i
kadar risk alır. 2x'te marj yarıya iner, nakit $7.750 → %0.78. Yani 1x
"daha az risk" değil, **niyet edilen %1'in altında kalmak.**

**Üç kapı da geçildi:** tam dönem %94.7→%128.7, 1. yarı %5.8→%12.4,
2. yarı %93.0→%114.0. Her dönemde 2x, 1x'i geçiyor.

**Neden 3x/5x değil:** likidasyon tamponu = %90/kaldıraç (`LIQ_BUFFER`).
1x→%90, 2x→%45, 3x→%30, 5x→%18. 3x'in %30'luk tamponu 1356 işlemde hiç
delinmedi; 2x onun 1.5 katı pay bırakıyor. 5x'te 8 likidasyon = duvar orada.
Kâğıt aşamasında marj bırakmayı seçtik; 3x, Faz 1 kapısından (~100 kapanmış
işlem) sonra canlı veriyle yeniden değerlendirilebilir.

**Açık pozisyonlara etkisi yok:** `leverage` yalnızca `_open()` içinde okunur;
açık pozisyonlar kendi `margin` değerlerini kayıtta taşır.

---

## 2026-09-07 — Panelden manuel stop değiştirme

Kullanıcı isteği: pozisyonun stop'unu panelden elle taşıyabilmek (4R'de
"DEVAM" senaryosunun tamamlayıcısı). Seçim: **tam serbest + onay** — hem
sıkma hem gevşetme mümkün, gevşetmede sert uyarı çıkar.

**Kaldırılamayan tek şey stop'un kendisi.** İki giriş koşulsuz reddedilir:
- stop ≤ 0 → "stop loss daima olacak" kuralı,
- pozisyonu ANINDA tetikleyecek seviye (LONG'da fiyatın üstü, SHORT'ta altı)
  → bu "kapat" demektir, onun için KAPAT düğmesi var.

**Sıkma tarafı bedava geldi:** `updated_trailing_stop` zaten cırcır
(`max(current_stop, aday)`), yani manuel sıkılan seviyeyi bot asla geri
gevşetmez. Kod değişikliği gerekmedi.

**Gevşetme tarafında gizli tuzak vardı.** İz süren stop her mumda
`uç değer ∓ 3×ATR` hesaplıyor. Gevşetme yapılırsa bot bunu bir sonraki mumda
geri alır ve verilen nefes payı en fazla 4 saat yaşar — verilen söz tutulmaz.
Çözüm: `Position.stop_manual_ref` kilidi. Kilit açıkken iz sürme atlanır
(uç değer takibi sürer).

**Kilit ne zaman açılmalı? İlk tasarım YANLIŞTI ve test yakaladı.** İlk kural
"aday eski stop'u geçince aç" idi. Test 10 bunu çürüttü: stop 109→105
gevşetildikten sonra fiyatın 112→113 yapması (1 dolarlık tepe, aday 110 > 109)
kilidi açıyor ve stop 110'a fırlıyordu — yani kullanıcının kaçınmak istediği
seviyeye geri dönülüyordu. Marjinal bir tik izni iptal ediyor.

Düzeltilmiş kural: **işlem, verilen nefes payını geri kazanmalı.**
`ref = eski + (eski − yeni) = 2×eski − yeni` (iki yön için de aynı formül).
Ne kadar pay istendiyse, algoritmanın kontrolü geri alması için işlemin o kadar
ilerlemesi gerekir. Örnek: 109→105 (4$ pay) → ref 113 → uç değer 116'yı
geçmeden kilit açılmaz. Kilit kendiliğinden açıldığı için "unutulup iz sürmesi
kapalı kalan pozisyon" riski de yok.

Mimari: panel pozisyona DOKUNMAZ, `cmd_<SEMBOL>` = `STOP:<fiyat>` yazar; tüm
geçerlilik denetimi botta yapılır (tek yazıcı ilkesi, mevcut KAPAT/manuel
aç akışıyla aynı).

Test: 19/19 geçti — geriye uyumluluk (eski JSON kaydı, `stop_manual_ref` yok),
reddedilen girişler (3), sıkma + cırcır koruması (3), gevşetme kilidi (4),
SHORT tarafı (5), aşırı gevşetmede ref≤0 nöbetçisi, komut kuyruğu (3).
