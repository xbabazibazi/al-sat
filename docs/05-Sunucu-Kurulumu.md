# Sunucu Kurulumu (7/24 çalışma) — UYGULANAN YÖNTEM

Bot **quonsoftware** sunucusunda çalışır; PC kapalı olsa bile işlem taramaya
devam eder. Panel Tailscale üzerinden her cihazdan (telefon dahil) erişilebilir.

## Ortam

| | |
|---|---|
| Sunucu | Ubuntu 26.04 LTS, fiziksel laptop |
| Erişim | `quon@100.85.134.94` (Tailscale) / `quon@192.168.2.83` (yerel ağ) |
| SSH anahtarı | `~/.ssh/id_ed25519_vaultwarden` |
| Proje yolu | `/home/quon/al-sat` |
| Panel | http://100.85.134.94:8484 |

## Mimari (Docker YOK, systemd YOK — sudo gerektirmez)

```
crontab (@reboot + 5 dakikalık nöbetçi)
   └── paneli-baslat.sh
         └── Panel (venv python, 8484)
               └── Bot (alt süreç, watchdog ile ayakta)
                     └── Binance API + Telegram
```

**Üç katmanlı süreklilik:**
1. `@reboot` — sunucu yeniden başlarsa 30 saniyede panel açılır
2. Nöbetçi cron (5 dk) — panel ölürse yeniden başlatır
3. Panel watchdog (20 sn) — bot ölürse yeniden başlatır, `bot_should_run`
   bayrağı SQLite'ta olduğu için DURDUR denene kadar açık kalır

Docker denendi (izin/grup/volume sorunları), systemd denendi (SSH'ta sudo TTY
sorunu) — ikisi de elendi. Sudo'suz yöntem sorunsuz çalıştı.

## Kurulum / güncelleme (tek komut)

```powershell
cd "D:\Projeler2\AL SAT BOT"
.\deploy\SUNUCUYA-KUR.ps1
```

Betik: kodu GitHub'a gönderir → sunucuda repoyu günceller → `.env` gönderir
→ venv kurar → paneli başlatır → botu otomatik çalıştırır → PC'deki botu durdurur.

## Yönetim

```powershell
$k = "$env:USERPROFILE\.ssh\id_ed25519_vaultwarden"
$s = "quon@100.85.134.94"

ssh -i $k $s "tail -n 40 ~/al-sat/logs/panel.log"      # panel logu
ssh -i $k $s "pgrep -af 'src.panel|src.main'"          # süreçler
ssh -i $k $s "crontab -l"                              # otomatik başlatma
ssh -i $k $s "cat ~/al-sat/data/bot_state.db > /dev/null; echo DB_OK"
```

Botu durdurmak/başlatmak için panelin **BAŞLAT / DURDUR** butonları kullanılır.

## Yedekleme

```powershell
scp -i $k ${s}:~/al-sat/data/bot_state.db "D:\Projeler2\AL SAT BOT\data\yedek.db"
```

## Notlar

- `.env` repoda yoktur, `scp` ile gönderilir (`chmod 600`)
- Panel yalnızca Tailscale IP'sini dinler (`PANEL_HOST`), internete kapalıdır
- PC'de bot çalıştırmayın — iki bot ayrı veritabanı kullanır, çakışır
