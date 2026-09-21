# Canlıya Geçiş — gerçek borsaya bağlanma planı

> Durum (2026-09-20): bot `futures_paper` modunda, sunucuda çalışıyor.
> **Gerçek para YOK.** Bu belge, oraya nasıl gidileceğini ve hangi kapıların
> hangi sırayla açılacağını anlatır.

## 1. Bugün nerede duruyoruz

### Hazır olan
| Parça | Dosya | Ne yapar |
|---|---|---|
| Vadeli emir geçidi | `src/futures_exchange.py` | Gerçek Binance USDT-M'e emir gönderir. Stop garantisi burada: `giris_ve_stop()` pozisyon+stop'u ayrılmaz yapar, `stop_tasi()` stopu korumasız bırakmadan taşır. |
| Üç kilit | `src/config.py` → `validate()` | Anahtar + `CANLI_ONAY=EVET_GERCEK_PARA` tam eşleşmesi + `RISK_PCT ≤ CANLI_RISK_TAVANI`. Kazara canlıya geçiş imkânsız. |
| Canlı öncesi kontrol | `src/canli_kontrol.py` | Saat sapması, API izinleri, pozisyon modu, bakiye, sembol filtreleri, STOP_MARKET kabulü. |
| Acil durdurma | `src/telegram_komut.py` | Telefondan `/kapat`, `/stop`, `/durum` — tek dokunuşla onaylı. |
| Panel | `src/panel.py` | BAŞLAT/DURDUR, manuel KAPAT/L/S/STOP, watchdog. |

### Eksik olan — KOD
1. ~~`FuturesLiveTrader` yok.~~ **2026-09-21'de yazıldı** (`src/futures_trader.py`,
   `FuturesPaperTrader`'ın alt sınıfı). `main.py` artık `futures_testnet`/
   `futures_live` modunda bunu kurup çalıştırıyor (eski "sessiz SPOT" reddi
   kaldırıldı, gerçek trader'a yönlendirildi). Kapsamı: atomik giriş+stop
   (`giris_ve_stop`), her turda stop nöbeti (`_stop_hit` içinde — kayıpsa
   derhal yeniden kurar, kuramazsa acil kapatır), iz süren stopun borsada
   taşınması (`stop_tasi`), kapanışta gerçek fiyat/PnL'in borsa işlem
   geçmişinden okunması (tahmine güvenmez), açılış mutabakatı, veri tazeliği
   kapısı. 11 testle doğrulandı (`tests/kritik_testler.py`, "CANLI TRADER").
   **Henüz TESTNET'te uçtan uca denenmedi** — testnet atlanmasına karar
   verildiği için ilk gerçek sınav küçük gerçek parayla olacak (bkz. §5/karar).
2. **Gün-içi düşüş kesici yok.** Devre kesici yalnızca günlük realize zararı sayar.
3. **Canlı günlük Telegram özeti yok.** Paper'daki özet $10.000 sanal
   başlangıç varsayıyor; canlı için ayrı yazılmadı (bilinçli, düşük öncelik).

> **2026-09-20'de kapatılan açık:** `BOT_MODE=futures_live` yazılsaydı `main.py`
> `else` dalına düşüyor, `build_broker()` **SPOT** brokerı kuruyor ve
> `SymbolTrader` ile **gerçek parayla spot** işlem açıyordu — kaldıraçsız,
> short'suz, kâğıtta doğrulanandan tamamen farklı bir strateji. Artık bu modda
> bot açıkça reddedip çıkıyor (`main.py`, test #124).

### Eksik olan — İSTATİSTİK
Faz 1 kapısı **≥100 kapanmış işlem** istiyor. **Şu an 21.**

| Ölçüm | Şu an (n=21) | Hedef bant | Durum |
|---|---|---|---|
| Kazanma oranı | %38.1 | %32–46 | ✅ bantta |
| Ödeme oranı (ort. kazanç / ort. zarar) | 2.05 | ≥1.8 | ✅ |
| İşlem başına beklenti | +8.52 USDT | >0 | ✅ |
| Tepeden düşüş | −458 USDT (≈%4.5) | ≤%20 | ✅ |

Sayılar doğru yönde **ama 21 işlem istatistiksel olarak anlamsız.** ~1.5
işlem/gün hızıyla 100'e ulaşmak yaklaşık **50 gün** daha sürer.

## 2. Mimari karar — tek strateji, iki uç

**Kâğıt ve canlı AYNI strateji kodunu çalıştırmalı.** Ayrı bir canlı trader
yazılırsa, 100 işlemlik kâğıt doğrulaması canlı hakkında hiçbir şey
kanıtlamaz — farklı kodun sonucunu ölçmüş oluruz.

Bu yüzden `FuturesLiveTrader`, `FuturesPaperTrader`'ın alt sınıfı olacak ve
yalnızca **borsaya dokunan dikişleri** override edecek:

| Dikiş | Kâğıtta | Canlıda |
|---|---|---|
| `_balance()` | KV'deki sanal sayı | `broker.bakiye_usdt()` |
| `_open()` | simüle dolum + slipaj | `broker.giris_ve_stop()` — atomik |
| `_close()` | simüle çıkış | `broker.pozisyonu_kapat()` |
| `_stop_hit()` | fiyat stopu geçti mi | **borsa tetikledi mi** (pozisyon yok oldu mu) |
| `_update_trailing()` | yalnız DB'ye yaz | `broker.stop_tasi()` → sonra DB |
| `account_equity()` | hesaplanan | borsadan okunan |
| funding | tahmin (`funding_acc`) | gerçek, borsadan |

Karar mantığı (`poll`, `_try_enter`, `assess`, `_process_manual_commands`,
devre kesici, pozisyon tavanı) **tek kopya kalır.**

### Kâğıtta karşılığı olmayan, canlıda ŞART olan davranışlar
1. **Açılış mutabakatı** — borsa gerçeği DB'yi ezer:
   - borsada var / DB'de yok → benimse (stopunu kur) veya kapat
   - DB'de var / borsada yok → stop tetiklenmiş, işlemi kapanmış yaz
   - ikisinde de var ama miktar/stop farklı → **borsa esas alınır**
2. **Her turda stop nöbeti** — `stop_var_mi()`: stop yoksa derhal kur;
   kurulamazsa pozisyonu kapat. (Stop elle iptal edilmiş olabilir.)
3. **Veri tazeliği kapısı** — son mum beklenenden eskiyse yeni giriş yok.
4. **Kısmi dolum gerçekliği** — dolan miktar istenenden az olabilir.

## 3. Sıra — hangi faz ne zaman

```
Faz 0  ✅ Sessiz-spot açığı kapatıldı (bitti)
Faz A  🔧 Kod: FuturesLiveTrader + mutabakat + tazelik kapısı
Faz B  🧪 TESTNET — gerçek borsa, sahte para (mekanik doğrulama)
Faz C  ⏳ İstatistik kapısı — 100 kapanmış kâğıt işlem   ← B ile PARALEL yürür
Faz D  💵 Küçük gerçek para ($100–200)
Faz E  📈 Ölçeklendirme
```

**Faz B ve C paralel yürür.** Testnet *mekaniği* doğrular (emir gidiyor mu,
stop borsada duruyor mu); kâğıt *istatistiği* toplar. İkisini sıraya dizmek
50 günü boşa harcamak olur.

### Faz B — Testnet çıkış ölçütleri
Testnet likiditesi sahtedir; **fiyat ve slipaj anlamsızdır.** Yalnızca şunlar
doğrulanır:
- [ ] Giriş emri gidiyor, dolum fiyatı doğru okunuyor
- [ ] STOP_MARKET borsada **gerçekten duruyor** (Binance arayüzünden görülüyor)
- [ ] İz süren stop taşınıyor; eski emir iptal ediliyor, boşluk kalmıyor
- [ ] Stop tetiklendiğinde bot bunu fark edip işlemi kapanmış yazıyor
- [ ] **Bot öldürülüp kaldırıldığında** mutabakat pozisyonu doğru buluyor
- [ ] Telegram `/kapat` gerçek emir gönderiyor
- [ ] Stop kurulamadığında pozisyon derhal kapanıyor (kasıtlı hata enjekte et)

### Faz D — Küçük gerçek para ayarları
| Ayar | Kâğıt | İlk canlı |
|---|---|---|
| Bakiye | $10.000 | **$100–200** |
| `RISK_PCT` | %2 | **%1** (tavan) |
| `MAX_CONCURRENT_POSITIONS` | 4 | **2** |
| Semboller | 10 | **2–3** (en likit: BTC, ETH, SOL) |
| `LEVERAGE` | 2x | 2x |

En az **1 ay** ve **≥20 kapanmış işlem** bu ayarlarla. Kâğıt ile canlı
sonuçlar birbirini tutuyor mu — asıl ölçülecek şey bu (slipaj farkı).

## 4. Binance tarafı hazırlık (kullanıcı yapar)

1. **Vadeli hesap** açık olmalı, USDT-M.
2. **Pozisyon modu: One-way (tek yön).** Hedge modda aynı sembolde hem long
   hem short açılabilir ve botun "tek pozisyon" varsayımı çöker.
3. **API anahtarı:**
   - ✅ Enable Futures
   - ❌ **Enable Withdrawals — ASLA açılmaz**
   - ✅ IP kısıtı = sunucunun **public çıkış IP'si** (Tailscale IP'si DEĞİL):
     ```
     ssh quon@100.85.134.94 "curl -s https://api.ipify.org"
     ```
4. **Sunucu saati:** >1000 ms sapma imzalı istekleri bozar.
   ```
   ssh quon@100.85.134.94 "timedatectl | grep -i synchronized"
   ```
5. **`.env`** (anahtarları **daima kullanıcı yazar**):
   ```
   BOT_MODE=futures_live
   BINANCE_LIVE_KEY=...
   BINANCE_LIVE_SECRET=...
   CANLI_ONAY=EVET_GERCEK_PARA
   RISK_PCT=0.01
   ```
   > Ayar değişikliği `git push` ile GİTMEZ — `deploy/SUNUCUYA-KUR.ps1` şart.
6. **Kontrolü çalıştır:**
   ```
   python -m src.canli_kontrol
   ```

## 5. Dürüst beklenti

- Ölçülen profil: **~%19.8 / 4 yıl**, Sharpe 0.58. Ortak bakiyeli portföy
  ölçümünde **gerçek MaxDD −%31.5** (korelasyon 0.68). $200 hesap bir ara
  $140 görebilir — bu arıza değil, stratejinin normali.
- **İşlemlerin ~%62'si zararla kapanır.** Kâr, az sayıda uzun süren
  kazançtan gelir. "Her şey stopla bitiyor" hissi bu stratejinin tasarımıdır.
- **Funding** uzun taşınan pozisyonlarda gerçek ve kalıcı bir maliyettir.
- 2x kaldıraçta likidasyon tamponu ≈ **%45**; stop ondan çok daha yakın,
  yani likidasyon beklenen bir sonuç değil — ama fitil riski sıfır değildir.
- **21 işlemle strateji değiştirmek, canlıya geçmekten daha büyük risktir.**

## 6. Değişmez kurallar

1. **Stopsuz pozisyon yoktur.** Stop Binance sunucusunda durur; bot ölse de çalışır.
2. **Para çekme izni asla açılmaz.**
3. **Anahtarları kullanıcı yazar**, asistan görmez.
4. **Borsa gerçeği DB'yi ezer**, tersi değil.
5. **Kâğıt ve canlı aynı strateji kodunu çalıştırır.**
