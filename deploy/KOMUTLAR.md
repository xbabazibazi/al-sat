# Çalıştırılacak Komutlar (PowerShell 5.1 uyumlu)

> `&&` PowerShell 5.1'de çalışmaz — komutları `;` ile ayır veya tek tek çalıştır.

## 1. Gitea remote'unu ekle (bir kez)

SSH config'inde `gitea` host'u zaten tanımlı, kullanıcı **quon**.
Önce Gitea arayüzünde `al-sat` adında **boş** repo aç, sonra:

```powershell
cd "D:\Projeler2\AL SAT BOT"
git remote add gitea gitea:quon/al-sat.git
git push gitea main
```

### Tek komutla hem GitHub hem Gitea'ya göndermek istersen

```powershell
cd "D:\Projeler2\AL SAT BOT"
git remote set-url --add --push origin https://github.com/xbabazibazi/al-sat.git
git remote set-url --add --push origin gitea:quon/al-sat.git
```

Bundan sonra `git push origin main` ikisine birden gönderir.

## 2. Sunucuya container kurulumu

Tek betik hepsini yapar (repo klonla + .env gönder + Docker kur + başlat):

```powershell
cd "D:\Projeler2\AL SAT BOT"
.\deploy\SUNUCUYA-KUR.ps1
```

Tailscale üzerinden bağlanamazsa yerel ağ adresini dene:

```powershell
.\deploy\SUNUCUYA-KUR.ps1 -Hedef quon@192.168.2.83
```

### Elle yapmak istersen (adım adım)

```powershell
# 1) Repoyu sunucuya klonla
ssh -i "$env:USERPROFILE\.ssh\id_ed25519_vaultwarden" quon@100.85.134.94 "git clone https://github.com/xbabazibazi/al-sat.git ~/al-sat"

# 2) .env dosyasini gonder (anahtarlar repoda yok)
scp -i "$env:USERPROFILE\.ssh\id_ed25519_vaultwarden" "D:\Projeler2\AL SAT BOT\.env" quon@100.85.134.94:~/al-sat/.env

# 3) Kurulumu calistir
ssh -i "$env:USERPROFILE\.ssh\id_ed25519_vaultwarden" quon@100.85.134.94 "cd ~/al-sat ; bash deploy/sunucuya-kur.sh"
```

## 3. Kurulumdan sonra

- Panel: **http://100.85.134.94:8484** (Tailscale'li her cihazdan, telefon dahil)
- Panelde **▶ BAŞLAT** → bot çalışır, **■ DURDUR** diyene kadar durmaz
- PC'deki botu durdur: yerel panelde (localhost:8484) **■ DURDUR**

## 4. Sunucudaki botu yönetme

```powershell
$s = "quon@100.85.134.94"; $k = "$env:USERPROFILE\.ssh\id_ed25519_vaultwarden"
ssh -i $k $s "cd ~/al-sat ; docker compose logs --tail 50"    # log
ssh -i $k $s "cd ~/al-sat ; docker compose ps"                # durum
ssh -i $k $s "cd ~/al-sat ; git pull ; docker compose up -d --build"  # guncelle
```

## Bilinen bilgiler

| Ne | Değer |
|---|---|
| Sunucu (Tailscale) | `quon@100.85.134.94` |
| Sunucu (yerel ağ) | `quon@192.168.2.83` |
| SSH anahtarı | `~/.ssh/id_ed25519_vaultwarden` |
| Gitea SSH | `gitea:quon/<proje>.git` (port 2222) |
| Gitea web | https://quonsoftware.tail1d1724.ts.net:3002 |
| GitHub | https://github.com/xbabazibazi/al-sat |
| Panel (sunucu) | http://100.85.134.94:8484 |
| Telegram | @Veksorbot |
