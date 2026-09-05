# Panel'i Windows Gorev Zamanlayici'ya kaydeder.
# Boylece bilgisayar acildiginda panel otomatik baslar ve hicbir
# oturuma bagimli olmaz. Panel de botu watchdog ile ayakta tutar.
#
# Kullanim:  .\deploy\GOREV-KUR.ps1
# Kaldirmak: Unregister-ScheduledTask -TaskName "AL-SAT Panel" -Confirm:$false

$ErrorActionPreference = "Stop"
$proje = Split-Path -Parent $PSScriptRoot
$gorevAdi = "AL-SAT Panel"

# Python yolunu bul
$python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command python.exe).Source }
Write-Host "Python: $python"
Write-Host "Proje : $proje"

$action = New-ScheduledTaskAction -Execute $python -Argument "-m src.panel" -WorkingDirectory $proje

# Oturum acilisinda basla + her 5 dakikada bir hala calisiyor mu diye kontrol
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)

Unregister-ScheduledTask -TaskName $gorevAdi -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask -TaskName $gorevAdi -Action $action -Trigger $triggerLogon `
    -Settings $settings -Description "AL-SAT bot paneli (botu watchdog ile ayakta tutar)" | Out-Null

Write-Host "`nGorev kaydedildi: '$gorevAdi'" -ForegroundColor Green
Write-Host "Simdi baslatiliyor..." -ForegroundColor Yellow
Start-ScheduledTask -TaskName $gorevAdi
Start-Sleep -Seconds 8

try {
    $j = (Invoke-WebRequest "http://localhost:8484/api/state" -UseBasicParsing -TimeoutSec 10).Content | ConvertFrom-Json
    Write-Host "Panel calisiyor: http://localhost:8484 (bot=$($j.bot_running))" -ForegroundColor Green
} catch {
    Write-Host "Panel henuz yanit vermiyor, birkac saniye sonra tarayicidan deneyin." -ForegroundColor Yellow
}

Write-Host "`nArtik bilgisayar her acildiginda panel otomatik baslayacak." -ForegroundColor Cyan
Write-Host "Panelde BASLAT'a bastiginizda bot da watchdog ile ayakta kalir." -ForegroundColor Cyan
