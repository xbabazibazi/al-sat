# AL-SAT BOT — TEK KOMUTLA SUNUCUYA KURULUM
#
# Ne yapar:
#   1) Bekleyen commitleri GitHub'a gonderir
#   2) Sunucuya baglanir, repoyu klonlar/gunceller
#   3) .env dosyasini guvenli sekilde gonderir
#   4) Docker container'i derler ve baslatir
#   5) BOTU OTOMATIK BASLATIR (panele girmene gerek yok)
#   6) Yerel PC'deki botu durdurur (cifte calisma olmasin)
#
# Kullanim:
#   cd "D:\Projeler2\AL SAT BOT"
#   .\deploy\SUNUCUYA-KUR.ps1
#
# Yerel ag uzerinden:
#   .\deploy\SUNUCUYA-KUR.ps1 -Hedef quon@192.168.2.83

param(
    [string]$Hedef = "quon@100.85.134.94",
    [string]$Anahtar = "$env:USERPROFILE\.ssh\id_ed25519_vaultwarden",
    [string]$RepoUrl = "https://github.com/xbabazibazi/al-sat.git",
    [switch]$PushAtlama
)

$proje = Split-Path -Parent $PSScriptRoot
$envDosya = Join-Path $proje ".env"
$sshOpt = @("-i", $Anahtar, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15")

function Baslik($m) { Write-Host "`n$m" -ForegroundColor Cyan }
function Ok($m)     { Write-Host "  OK  $m" -ForegroundColor Green }
function Bilgi($m)  { Write-Host "      $m" -ForegroundColor Gray }
function Hata($m)   { Write-Host "  !!  $m" -ForegroundColor Red }

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  AL-SAT BOT - sunucuya otomatik kurulum" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

if (-not (Test-Path $envDosya)) { Hata ".env bulunamadi: $envDosya"; exit 1 }

# --- 1) Commitleri gonder ---
if (-not $PushAtlama) {
    Baslik "[1/6] Kod GitHub'a gonderiliyor..."
    Set-Location $proje
    git push origin main 2>&1 | ForEach-Object { Bilgi $_ }
    $pushKod = $LASTEXITCODE
    # Sunucu dosyalari BU PC'den almiyor, GitHub'dan CEKIYOR. Yani push
    # basarisizsa dagitilan sey ESKI koddur. Eski surum bunu yutuyordu:
    # "push atlandi (zaten guncel olabilir)" deyip devam ediyor, kullanici
    # dagitimin gittigini saniyordu. 2026-09-09'da tam bu yuzden d002bef
    # sunucuya gitmedi ve saatlerce yanlis yerde arandi.
    # Dogrulama: yereldeki HEAD gercekten uzakta mi?
    $yerel = (git rev-parse HEAD).Trim()
    $uzak  = ((git ls-remote origin refs/heads/main) -split "\s+")[0]
    if ($yerel -ne $uzak) {
        Hata "PUSH GITMEDI - dagitim durduruldu."
        Bilgi "yerel HEAD : $yerel"
        Bilgi "GitHub main: $uzak"
        if ($pushKod -ne 0) { Bilgi "git push cikis kodu: $pushKod" }
        Bilgi ""
        Bilgi "En sik sebep: remote.origin.pushurl'de birden fazla adres var"
        Bilgi "(orn. ulasilamayan bir gitea). Push hepsine gider, biri patlarsa"
        Bilgi "komut hata doner. Kontrol ve temizlik:"
        Bilgi "    git config --get-all remote.origin.pushurl"
        Bilgi "    git config --unset-all remote.origin.pushurl"
        Bilgi ""
        Bilgi "Eski kodu dagitmamak icin cikiliyor. Duzeltip tekrar calistir."
        exit 1
    }
    Ok "push tamam - GitHub main = $($yerel.Substring(0,7))"
} else {
    Baslik "[1/6] Push atlandi (-PushAtlama)"
}

# --- 2) SSH testi ---
Baslik "[2/6] Sunucuya baglaniliyor ($Hedef)..."
$t = ssh @sshOpt $Hedef "echo TAMAM" 2>&1
if ($LASTEXITCODE -ne 0 -or "$t" -notmatch "TAMAM") {
    Hata "Baglanti kurulamadi: $t"
    Bilgi "Yerel agi dene:  .\deploy\SUNUCUYA-KUR.ps1 -Hedef quon@192.168.2.83"
    exit 1
}
Ok "baglandi"

# --- 3) Repo ---
Baslik "[3/6] Repo sunucuya aliniyor..."
$k = "if [ -d ~/al-sat/.git ]; then cd ~/al-sat && git fetch --all && git reset --hard origin/main; else git clone $RepoUrl ~/al-sat; fi"
ssh @sshOpt $Hedef $k 2>&1 | ForEach-Object { Bilgi $_ }
if ($LASTEXITCODE -ne 0) { Hata "Repo alinamadi"; exit 1 }
Ok "repo hazir"

# --- 4) .env gonder ---
Baslik "[4/6] .env gonderiliyor (anahtarlar repoda yok)..."
scp -i $Anahtar -o StrictHostKeyChecking=accept-new $envDosya "${Hedef}:~/al-sat/.env" 2>&1 | ForEach-Object { Bilgi $_ }
if ($LASTEXITCODE -ne 0) { Hata ".env gonderilemedi"; exit 1 }
ssh @sshOpt $Hedef "chmod 600 ~/al-sat/.env"
Ok ".env yerlestirildi (chmod 600)"

# --- 5) Kurulum + botu otomatik baslat ---
Baslik "[5/6] Servis kuruluyor ve bot baslatiliyor..."
Bilgi "(sudo gerektirmez: kullanici-yerel python + crontab)"
ssh @sshOpt $Hedef "cd ~/al-sat ; bash deploy/kur-sunucu.sh" 2>&1 | ForEach-Object { Bilgi $_ }

# --- 6) Dogrula + yerel botu durdur ---
Baslik "[6/6] Dogrulama..."
Start-Sleep -Seconds 5
$panelUrl = "http://" + ($Hedef -split "@")[1] + ":8484"
try {
    $j = (Invoke-WebRequest "$panelUrl/api/state" -UseBasicParsing -TimeoutSec 20).Content | ConvertFrom-Json
    Ok "Sunucudaki bot: calisiyor=$($j.bot_running) | mod=$($j.mode) | $($j.leverage)x | varlik=`$$($j.equity)"

    # Yerel botu durdur - iki bot ayni anda calismasin
    try {
        Invoke-WebRequest "http://localhost:8484/api/stop" -Method POST -UseBasicParsing -TimeoutSec 8 | Out-Null
        Ok "Yerel PC botu durduruldu (cifte calisma engellendi)"
    } catch { Bilgi "Yerel bot zaten kapali" }
} catch {
    Hata "Panel yanit vermedi: $panelUrl"
    Bilgi "Sunucudaki loglar aliniyor..."
    ssh @sshOpt $Hedef "cd ~/al-sat ; echo '--- panel log ---' ; tail -n 40 logs/panel.log 2>/dev/null ; echo '--- surecler ---' ; pgrep -af 'src.panel|src.main' || echo 'calisan yok'" 2>&1 | ForEach-Object { Bilgi $_ }
    exit 1
}

Write-Host "`n==================================================" -ForegroundColor Green
Write-Host "  KURULUM TAMAM - BOT SUNUCUDA CALISIYOR" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Write-Host "  Panel : $panelUrl" -ForegroundColor White
Write-Host "  (Tailscale'li her cihazdan - telefon dahil)" -ForegroundColor Gray
Write-Host ""
Write-Host "  Bot zaten basladi. Panelden DURDUR demedikce" -ForegroundColor White
Write-Host "  calisir; sunucu yeniden basladiginda geri gelir." -ForegroundColor White
Write-Host "  PC'ni kapatabilirsin." -ForegroundColor White
Write-Host "==================================================" -ForegroundColor Green
