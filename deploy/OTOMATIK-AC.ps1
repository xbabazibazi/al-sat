# AL-SAT BOT — OTOMATIK GUNCELLEMEYI AC / KAPAT / DURUMUNU GOR
#
# Ne yapar: sunucudaki crontab'a 5 dakikalik bir kontrol ekler. Bundan sonra
# uzak repoya itilen her KOD degisikligi, sunucu tarafindan kendiliginden
# alinir; once testler calisir, gecerse bot yeniden baslar.
#
# SIRA ONEMLI — once kodu sunucuya goster:
#   .\deploy\SUNUCUYA-KUR.ps1      (betigi ve .env'i sunucuya tasir)
#   .\deploy\OTOMATIK-AC.ps1       (otomatigi acar)
#
# Kullanim:
#   .\deploy\OTOMATIK-AC.ps1              # ac
#   .\deploy\OTOMATIK-AC.ps1 -Durum       # acik mi, son ne yapmis
#   .\deploy\OTOMATIK-AC.ps1 -Kapat       # kapat
#   .\deploy\OTOMATIK-AC.ps1 -SimdiCalistir   # beklemeden bir kez calistir
#
# NOT: .env sunucuya bu yolla GITMEZ. LEVERAGE / SYMBOLS / RISK_PCT gibi AYAR
# degisikliklerinde her zaman SUNUCUYA-KUR.ps1 gerekir. Otomatik akis yalnizca
# KOD tasir.

param(
    [string]$Hedef  = "quon@100.85.134.94",
    [string]$Anahtar = "$env:USERPROFILE\.ssh\id_ed25519_vaultwarden",
    [switch]$Kapat,
    [switch]$Durum,
    [switch]$SimdiCalistir
)

$sshOpt = @("-i", $Anahtar, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15")
$betik  = "~/al-sat/deploy/sunucu-otomatik-guncelle.sh"

function Baslik($m) { Write-Host "`n$m" -ForegroundColor Cyan }
function Ok($m)     { Write-Host "  OK  $m" -ForegroundColor Green }
function Bilgi($m)  { Write-Host "      $m" -ForegroundColor Gray }
function Hata($m)   { Write-Host "  !!  $m" -ForegroundColor Red }

# --- baglanti testi ---
Baslik "Sunucuya baglaniliyor ($Hedef)..."
$t = ssh @sshOpt $Hedef "echo TAMAM" 2>&1
if ($LASTEXITCODE -ne 0 -or "$t" -notmatch "TAMAM") {
    Hata "Baglanti kurulamadi: $t"
    Bilgi "Yerel agi dene:  .\deploy\OTOMATIK-AC.ps1 -Hedef quon@192.168.2.83"
    exit 1
}
Ok "baglandi"

# --- betik sunucuda var mi? ---
$v = ssh @sshOpt $Hedef "test -f $betik && echo VAR || echo YOK" 2>&1
if ("$v" -notmatch "VAR") {
    Hata "Guncelleme betigi sunucuda YOK."
    Bilgi "Once sunu calistir:  .\deploy\SUNUCUYA-KUR.ps1"
    exit 1
}

if ($Durum) {
    Baslik "Cron kaydi:"
    ssh @sshOpt $Hedef "crontab -l 2>/dev/null | grep otomatik-guncelle || echo '  (KAPALI - kayit yok)'" |
        ForEach-Object { Bilgi $_ }
    Baslik "Son gunlukler:"
    ssh @sshOpt $Hedef "tail -n 25 ~/al-sat/logs/otomatik-guncelle.log 2>/dev/null || echo '  (henuz calismamis)'" |
        ForEach-Object { Bilgi $_ }
    Baslik "Sunucudaki surum:"
    ssh @sshOpt $Hedef "cd ~/al-sat && git log --oneline -1" | ForEach-Object { Bilgi $_ }
    exit 0
}

if ($Kapat) {
    Baslik "Otomatik guncelleme KAPATILIYOR..."
    ssh @sshOpt $Hedef "crontab -l 2>/dev/null | grep -v otomatik-guncelle | crontab -" |
        ForEach-Object { Bilgi $_ }
    Ok "kapatildi (bot calismaya devam ediyor, sadece otomatik guncelleme durdu)"
    exit 0
}

if ($SimdiCalistir) {
    Baslik "Beklemeden bir kez calistiriliyor..."
    ssh @sshOpt $Hedef "bash $betik" 2>&1 | ForEach-Object { Bilgi $_ }
    Ok "bitti"
    exit 0
}

# --- ac ---
Baslik "Otomatik guncelleme ACILIYOR..."
ssh @sshOpt $Hedef "bash $betik --kur" 2>&1 | ForEach-Object { Bilgi $_ }
if ($LASTEXITCODE -ne 0) { Hata "Kurulum basarisiz"; exit 1 }

Write-Host "`n==================================================" -ForegroundColor Green
Write-Host "  OTOMATIK GUNCELLEME ACIK" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Write-Host "  Sunucu her 5 dakikada bir yeni KOD var mi bakar." -ForegroundColor White
Write-Host "  Once 30 kritik test calisir; GECMEZSE eski surume" -ForegroundColor White
Write-Host "  doner ve Telegram'a uyari atar. Gecerse bot yeniden" -ForegroundColor White
Write-Host "  baslar ve acik pozisyonlar dogrulanir." -ForegroundColor White
Write-Host ""
Write-Host "  Durum   : .\deploy\OTOMATIK-AC.ps1 -Durum" -ForegroundColor Gray
Write-Host "  Kapat   : .\deploy\OTOMATIK-AC.ps1 -Kapat" -ForegroundColor Gray
Write-Host ""
Write-Host "  UNUTMA: .env (LEVERAGE, SYMBOLS, RISK_PCT...) bu yolla" -ForegroundColor Yellow
Write-Host "  GITMEZ. Ayar degistirirsen SUNUCUYA-KUR.ps1 calistir." -ForegroundColor Yellow
Write-Host "==================================================" -ForegroundColor Green
