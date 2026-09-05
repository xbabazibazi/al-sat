# AL-SAT BOT — PC'de kalici kurulum (TEK KOMUT)
#
# Ne yapar:
#   1) Bagimliliklari kontrol eder
#   2) Paneli Windows Gorev Zamanlayici'ya kaydeder
#      -> bilgisayar acildiginda otomatik baslar
#      -> cokerse 1 dakikada geri gelir
#      -> hicbir oturuma/pencereye bagli degildir
#   3) Paneli baslatir, botu otomatik calistirir
#   4) Uyku ayarini kontrol eder (bot uyurken islem kaciririr)
#
# Kullanim:
#   cd "D:\Projeler2\AL SAT BOT"
#   .\KUR.ps1

$ErrorActionPreference = "Continue"
$proje = $PSScriptRoot
$gorevAdi = "AL-SAT Panel"

function Ok($m)    { Write-Host "  [OK] $m" -ForegroundColor Green }
function Bilgi($m) { Write-Host "       $m" -ForegroundColor Gray }
function Uyari($m) { Write-Host "  [!] $m" -ForegroundColor Yellow }

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  AL-SAT BOT - PC kurulumu" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# --- 1) Bagimliliklar ---
Write-Host "`n[1/4] Bagimliliklar kontrol ediliyor..." -ForegroundColor Yellow
$eksik = python -c "
import sys
eksik = []
for m in ('pandas','numpy','requests','dotenv','binance'):
    try: __import__(m)
    except ImportError: eksik.append(m)
print(','.join(eksik))
" 2>$null
if ($eksik) {
    Bilgi "Eksik paketler kuruluyor: $eksik"
    python -m pip install --quiet -r (Join-Path $proje "requirements.txt")
}
Ok "Python paketleri hazir"

if (-not (Test-Path (Join-Path $proje ".env"))) {
    Uyari ".env dosyasi yok! .env.example'i kopyalayip doldurun."
    exit 1
}
Ok ".env bulundu"

# --- 2) Gorev Zamanlayici ---
Write-Host "`n[2/4] Otomatik baslatma kuruluyor..." -ForegroundColor Yellow
$python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command python.exe).Source }

try {
    $action = New-ScheduledTaskAction -Execute $python -Argument "-m src.panel" -WorkingDirectory $proje
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Days 0)

    Unregister-ScheduledTask -TaskName $gorevAdi -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $gorevAdi -Action $action -Trigger $trigger `
        -Settings $settings -Description "AL-SAT bot paneli" -ErrorAction Stop | Out-Null
    Ok "Gorev kaydedildi: '$gorevAdi' (her acilista otomatik baslar)"
    $gorevVar = $true
} catch {
    Uyari "Gorev kaydedilemedi: $($_.Exception.Message)"
    Bilgi "Panel yine de baslatilacak, ama PC yeniden baslarsa BASLAT.bat'a cift tiklayin."
    $gorevVar = $false
}

# --- 3) Baslat ---
Write-Host "`n[3/4] Panel ve bot baslatiliyor..." -ForegroundColor Yellow
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*src.panel*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

if ($gorevVar) {
    Start-ScheduledTask -TaskName $gorevAdi
} else {
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c","start `"AL-SAT PANEL`" cmd /k `"python -m src.panel`"" -WindowStyle Hidden
}

$hazir = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 2
    try {
        $j = (Invoke-WebRequest "http://localhost:8484/api/state" -UseBasicParsing -TimeoutSec 5).Content | ConvertFrom-Json
        $hazir = $true; break
    } catch { }
}

if (-not $hazir) {
    Uyari "Panel yanit vermedi. Elle deneyin:  python -m src.panel"
    exit 1
}
Ok "Panel calisiyor: http://localhost:8484"

# Botu otomatik baslat
try {
    Invoke-WebRequest "http://localhost:8484/api/start" -Method POST -UseBasicParsing -TimeoutSec 20 | Out-Null
    Start-Sleep -Seconds 10
    $j = (Invoke-WebRequest "http://localhost:8484/api/state" -UseBasicParsing -TimeoutSec 10).Content | ConvertFrom-Json
    if ($j.bot_running) {
        Ok "Bot calisiyor | $($j.leverage)x | $($j.symbols.Count) parite | gunluk stop %$($j.max_daily_loss)"
    } else {
        Uyari "Bot baslamadi - panelden BASLAT'a basin"
    }
} catch { Uyari "Bot otomatik baslatilamadi - panelden BASLAT'a basin" }

# --- 4) Uyku kontrolu ---
Write-Host "`n[4/4] Guc ayarlari kontrol ediliyor..." -ForegroundColor Yellow
$uyku = (powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 2>$null | Select-String "Current AC Power Setting Index").ToString()
if ($uyku -match "0x00000000") {
    Ok "Bilgisayar uyumuyor - bot kesintisiz calisir"
} else {
    Uyari "Bilgisayar uykuya geciyor! Bot uyurken islem kacirir."
    Bilgi "Kapatmak icin (yonetici olarak):"
    Bilgi "  powercfg /change standby-timeout-ac 0"
}

Write-Host "`n==================================================" -ForegroundColor Green
Write-Host "  KURULUM TAMAM" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Write-Host "  Panel    : http://localhost:8484" -ForegroundColor White
Write-Host "  Telegram : @Veksorbot (islem bildirimleri)" -ForegroundColor White
Write-Host "  Bot zaten calisiyor; panelden DURDUR demedikce durmaz." -ForegroundColor White
Write-Host "  PC her acildiginda panel otomatik baslar." -ForegroundColor White
Write-Host "==================================================" -ForegroundColor Green
