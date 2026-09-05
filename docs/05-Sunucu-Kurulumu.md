# Sunucu Kurulumu (7/24 çalışma)

Bot kendi PC'nde değil, **quonsoftware** sunucusunda çalışır; PC'n kapansa bile
işlem taramaya devam eder. Panel Tailscale üzerinden her cihazdan erişilebilir.

## Mimari

```
[ quonsoftware sunucusu (Linux, Tailscale 100.85.134.94) ]
        │
        └── Docker konteyneri (restart: always)
              └── Panel (8484)  ──►  BAŞLAT/DURDUR + watchdog
                    └── Bot (alt süreç)  ──►  Binance API + Telegram
        │
        └── Veri: ./data (SQLite — pozisyon, işlem geçmişi kalıcı)

Erişim:  https://100.85.134.94:8484  (yalnızca Tailscale ağından)
```

**Üç katmanlı süreklilik:**
1. `restart: always` — sunucu yeniden başlasa konteyner geri gelir
2. Watchdog — bot düşerse 45 saniyede yeniden başlatılır
3. `bot_should_run` bayrağı — BAŞLAT dedikten sonra DURDUR diyene kadar açık kalır

## Kurulum adımları

### 1. Repoyu sunucuya al

```bash
ssh quonsoftware
git clone https://github.com/xbabazibazi/al-sat.git ~/al-sat
cd ~/al-sat
```

(veya Gitea'dan: `git clone https://quonsoftware.tail1d1724.ts.net:3002/<kullanici>/al-sat.git ~/al-sat`)

### 2. .env dosyasını kopyala

`.env` repoda **yoktur** (Telegram token ve ayarlar içerir). Yerel makinenden gönder:

```powershell
scp "D:\Projeler2\AL SAT BOT\.env" quonsoftware:~/al-sat/.env
```

### 3. Kurulum betiğini çalıştır

```bash
cd ~/al-sat
bash deploy/sunucuya-kur.sh
```

Betik: Docker'ı kurar (yoksa), Tailscale IP'sini bulup porta bağlar,
konteyneri derleyip başlatır.

### 4. Paneli aç ve başlat

Tarayıcıda **http://100.85.134.94:8484** → **▶ BAŞLAT**

## Yönetim komutları

```bash
docker compose logs -f          # canlı log
docker compose ps               # durum
docker compose restart          # yeniden başlat
docker compose down             # tamamen durdur
docker compose up -d --build    # kod güncellemesinden sonra
```

## Güvenlik notları

- Panel portu **Tailscale IP'sine kilitlidir** (`100.85.134.94:8484:8484`) —
  internete açık değildir, yalnızca kendi cihazlarından erişilir.
- `.env` dosyası repoya girmez (`.gitignore`). Sunucuda `chmod 600 .env` yap.
- Bot `futures_paper` modunda sanal parayla çalışır — gerçek borsa emri göndermez.

## PC'deki botu kapatmayı unutma

Sunucuda çalışmaya başladıktan sonra PC'ndeki botu durdur (çifte işlem olmasın):
yerel panelde **■ DURDUR** veya BASLAT.bat pencerelerini kapat.
Not: iki bot aynı `data/bot_state.db` dosyasını paylaşmadığı için çifte
çalışma kilidi bu durumda korumaz — biri PC'de biri sunucuda ayrı hesaplar olur.
