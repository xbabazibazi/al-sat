# AL-SAT BOT — sunucuya kurulum (Windows tarafindan calistirilir)
#
# Kullanim:
#   cd "D:\Projeler2\AL SAT BOT"
#   .\deploy\SUNUCUYA-KUR.ps1
#
# Farkli kullanici adi gerekiyorsa:
#   .\deploy\SUNUCUYA-KUR.ps1 -Kullanici mustafa

param(
    # Sunucu bilgileri SSH config'inden tespit edildi:
    #   Host vaultwarden -> 192.168.2.83, User quon, id_ed25519_vaultwarden
    #   Ayni makine Tailscale'de: quonsoftware = 100.85.134.94
    [string]$Hedef = "quon@100.85.134.94",
    [string]$Anahtar = "$env:USERPROFILE\.ssh\id_ed25519_vaultwarden",
    [string]$RepoUrl = "https://github.com/xbabazibazi/al-sat.git"
)

$ErrorActionPreference = "Continue"
$proje = Split-Path -Parent $PSScriptRoot
$envDosya = Join-Path $proje ".env"

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  AL-SAT BOT - sunucuya kurulum" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# --- 0) .env var mi? ---
if (-not (Test-Path $envDosya)) {
    Write-Host "HATA: .env dosyasi bulunamadi: $envDosya" -ForegroundColor Red
    exit 1
}

# --- 1) SSH baglantisi test ---
Write-Host "`n[1/4] SSH baglantisi test ediliyor ($Hedef)..." -ForegroundColor Yellow

$sshOpt = @("-i", $Anahtar, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10")
$sonuc = ssh @sshOpt $Hedef "echo TAMAM" 2>&1
if ($LASTEXITCODE -ne 0 -or "$sonuc" -notmatch "TAMAM") {
    Write-Host "Baglanti kurulamadi: $sonuc" -ForegroundColor Red
    Write-Host "`nAlternatif: yerel ag adresini dene" -ForegroundColor Yellow
    Write-Host "  .\deploy\SUNUCUYA-KUR.ps1 -Hedef quon@192.168.2.83" -ForegroundColor White
    exit 1
}
Write-Host "      BAGLANDI" -ForegroundColor Green
$hedef = $Hedef

# --- 2) Repoyu klonla / guncelle ---
Write-Host "`n[2/4] Repo sunucuya aliniyor..." -ForegroundColor Yellow
$klonKomut = "if [ -d ~/al-sat/.git ]; then cd ~/al-sat && git pull; else git clone $RepoUrl ~/al-sat; fi"
ssh @sshOpt $hedef $klonKomut
if ($LASTEXITCODE -ne 0) {
    Write-Host "Repo alinamadi. GitHub reposu private ise sunucuda erisim gerekir." -ForegroundColor Red
    exit 1
}

# --- 3) .env dosyasini gonder (anahtarlar repoda yok) ---
Write-Host "`n[3/4] .env dosyasi gonderiliyor..." -ForegroundColor Yellow
scp -i $Anahtar -o StrictHostKeyChecking=accept-new $envDosya "${hedef}:~/al-sat/.env"
if ($LASTEXITCODE -ne 0) {
    Write-Host ".env gonderilemedi." -ForegroundColor Red
    exit 1
}
ssh @sshOpt $hedef "chmod 600 ~/al-sat/.env"

# --- 4) Kurulum betigini calistir ---
Write-Host "`n[4/4] Container kuruluyor (Docker gerekirse yuklenir)..." -ForegroundColor Yellow
ssh @sshOpt $hedef "cd ~/al-sat && bash deploy/sunucuya-kur.sh"

Write-Host "`n==================================================" -ForegroundColor Cyan
Write-Host "  Kurulum akisi tamamlandi" -ForegroundColor Cyan
Write-Host "  Panel: http://100.85.134.94:8484" -ForegroundColor Green
Write-Host "  Panelde BASLAT'a bas - bot calisir, DURDUR diyene kadar durmaz." -ForegroundColor White
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "`nUNUTMA: sunucuda calismaya baslayinca PC'deki botu durdur" -ForegroundColor Yellow
Write-Host "(yerel panelde DURDUR) - iki bot ayri hesap gibi calisir." -ForegroundColor Yellow
