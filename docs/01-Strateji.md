# Strateji Kuralları

## Temel felsefe: kırılımı takip et, tersine dönüşü avlama

Bot **trend takipçisidir**, ortalamaya dönüşçü değildir:

| | Bizim yaptığımız | Yaygın sezgi (test edildi, elendi) |
|---|---|---|
| Long | Fiyat son 10 mumun **tepesini yukarı kırınca** | Dipten al |
| Short | Fiyat son 10 mumun **dibini aşağı kırınca** | Tepeden sat |

Sebep: kripto trendli bir piyasa. Yeni zirve, çoğu zaman daha yüksek zirvelerin
habercisi. "Dipten al" ise düşen bıçağı tutmaktır — dip, çoğu zaman daha derin
dibin başlangıcıdır. Bu bir tercih değil, ölçüm sonucu — bkz.
[Backtest Sonuçları](02-Backtest-Sonuclari.md#sezgi-testi-tepeden-short-dipten-long).

## Giriş kuralları

**LONG** (üç şart birden):
1. Kapanış > EMA200 (trend yukarı)
2. Kapanış > son 10 mumun en yükseği (Donchian kırılımı)
3. RSI < 80 (zirvede kapitülasyon alımı yapma)

**SHORT** (aynanın yansıması):
1. Kapanış < EMA200
2. Kapanış < son 10 mumun en düşüğü
3. RSI > 20

Her iki durumda da **ön değerlendirme onayı** şarttır (aşağıda).

## Ön değerlendirme (5 araç)

Her mum kapanışında çalışır, skor üretir. Skor ≥ +50 → long serbest,
≤ −50 → short serbest, arası → bekle.

| Araç | Ağırlık | Ne ölçer |
|---|---|---|
| 4s Trend | ±30 | Fiyatın EMA200'e göre konumu |
| Günlük Trend | ±20 | Üst zaman dilimi teyidi |
| Momentum (RSI) | ±15 | Alıcı/satıcı baskınlığı |
| Trend Gücü | ±20 | EMA200 eğimi (ATR birimiyle) |
| Volatilite | VETO | ATR %0.3 altı (ölü) veya %6 üstü (kaotik) → işlem yok |

Kırılım sinyali olsa bile analiz onaylamazsa **giriş reddedilir** ve loglanır.

## Çıkış: yalnızca ATR izleyen stop

- İlk stop: giriş ± (ATR × 3.0)
- Fiyat lehe gittikçe stop takip eder, **asla geri gitmez**
- Sabit kâr-al hedefi YOKTUR — büyük trendler sonuna kadar sağılır
- Sonuç: kazanma oranı düşük (~%40) ama ortalama kazanç ortalama kaybın 2 katı

## Parametreler

| Ayar | Değer | Not |
|---|---|---|
| Zaman dilimi | 4 saat | 1h/15m/5m test edildi, komisyona yenildi |
| Donchian | 10 | Aktif mod. 20 = sabırlı mod (daha az işlem, yüksek Sharpe) |
| ATR çarpanı | 3.0 | 2022-2026 taramasında sağlam bölge |
| Kaldıraç | 2x | 3x+ katkı üretmedi |
| Risk / işlem | %2 | Boyutu bu belirler, kaldıraç değil |
| Pariteler | BTC, ETH, SOL, BNB | Kârlılığı kanıtlananlar |
