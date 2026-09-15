#!/usr/bin/env bash
# OTOMATIK GUNCELLEME — sunucunun kendisi calistirir (cron, 5 dakikada bir).
#
# NEDEN BOYLE: gelistirici makinesinden sunucuya ssh ile deploy etmek her zaman
# mumkun olmuyor. Akis tersine cevrildi: kod GitHub'a itilir, SUNUCU kendisi
# ceker. Boylece disaridan baglanti gerekmez.
#
# GUVENLIK KAPISI — bu betigin varlik sebebi:
#   Yeni kod, testler GECMEDEN canli bota UYGULANMAZ. Test basarisiz olursa
#   calisan surum oldugu gibi birakilir ve Telegram'a uyari gider. Yani "her
#   commit otomatik canliya gider" DEGIL, "her commit once sinanir" demektir.
#
# ACIK POZISYON GUVENLIGI:
#   Guncelleme oncesi acik pozisyonlarin giris fiyatlari kaydedilir, sonrasinda
#   yeniden okunup KARSILASTIRILIR. Fark varsa alarm gider. Kullanicinin degismez
#   sarti: "guncelleme pozisyonu kapatmasin, eski verisiyle devam etsin."
#
# NE YAPAMAZ:
#   .env dosyasi git'te DEGILDIR (anahtarlar orada). LEVERAGE, SYMBOLS,
#   RISK_PCT gibi AYAR degisiklikleri bu yolla GELMEZ — onlar icin hala
#   deploy/SUNUCUYA-KUR.ps1 gerekir. Bu betik yalnizca KOD tasir.
#
# Kurulum (sunucuda BIR KEZ):
#   bash ~/al-sat/deploy/sunucu-otomatik-guncelle.sh --kur
#
# Elle calistirma / test:
#   bash ~/al-sat/deploy/sunucu-otomatik-guncelle.sh
#
# DOGRULAMA (2026-09-09): kilit sizintisi (fd 9) duzeltmesi sunucuda dogrulandi —
# kilit_serbest=0, kalp atisi saglikli. Bu satirin kendisi ucecu kanarya taahhudu:
# sunucu bunu KENDI cekerse dongu ucdan uca kanitlanmis olur.
set -uo pipefail

PROJE="${HOME}/al-sat"
LOG="${PROJE}/logs/otomatik-guncelle.log"
DAL="${DEPLOY_DAL:-main}"

mkdir -p "${PROJE}/logs"
kayit() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

PY="${PROJE}/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

# PANEL ADRESI SABIT YAZILAMAZ. deploy/kur-sunucu.sh, .env icindeki PANEL_HOST'u
# Tailscale IP'sine sabitler; yani panel 127.0.0.1'de DINLEMEZ. Ilk surumde
# adres 127.0.0.1 sabit yazilmisti ve sonuc sinsi oldu: pozisyon dogrulamasi
# hep basarisiz oldu, guvenlik kapisi her seferinde devreye girdi ve guncelleme
# SESSIZCE ertelendi. Adres artik .env'den okunuyor.
PANEL=$("$PY" - <<PYEOF 2>/dev/null
import sys
sys.path.insert(0, "${PROJE}")
from src.config import CONFIG
h = CONFIG.panel_host
if h in ("0.0.0.0", "", "::"):     # her arayuzde dinliyorsa yerelden sor
    h = "127.0.0.1"
print(f"http://{h}:{CONFIG.panel_port}")
PYEOF
)
[ -n "$PANEL" ] || PANEL="http://127.0.0.1:8484"

# Telegram bildirimi. Anahtarlar .env'de; onlari kabuga tasimayalim diye
# mesaj ortam degiskeniyle gecirilir ve Python .env'i kendisi okur.
bildir() {
  local py="${PY:-python3}"
  BILDIR_PROJE="$PROJE" "$py" - <<'PYEOF' 2>/dev/null || true
import os, sys
sys.path.insert(0, os.environ["BILDIR_PROJE"])
from src.config import CONFIG
from src.notifier import TelegramNotifier
n = TelegramNotifier(CONFIG.telegram_token, CONFIG.telegram_chat_id)
getattr(n, os.environ.get("BILDIR_TIP", "send"))(os.environ.get("BILDIR_MESAJ", ""))
PYEOF
}

# ---------------------------------------------------------------- tek-kez kurulum
if [ "${1:-}" = "--kur" ]; then
  SATIR="*/5 * * * * bash ${PROJE}/deploy/sunucu-otomatik-guncelle.sh >/dev/null 2>&1"
  TMP=$(mktemp)
  crontab -l 2>/dev/null | grep -v "sunucu-otomatik-guncelle" > "$TMP" || true
  echo "$SATIR" >> "$TMP"
  crontab "$TMP" && rm -f "$TMP"
  echo "Kuruldu. Her 5 dakikada bir '${DAL}' dali kontrol edilecek."
  echo "Kapatmak icin: crontab -e  (sunucu-otomatik-guncelle satirini sil)"
  crontab -l | grep otomatik-guncelle
  exit 0
fi

cd "$PROJE" || { kayit "HATA: $PROJE yok"; exit 1; }

# KALP ATISI — KILITTEN ONCE yazilir. Betik yapacak is yokken sessizce cikiyor;
# bu dogru ama yan etkisi sinsi: "cron gercekten calisiyor mu" disaridan
# anlasilamiyor. 2026-09-09'da cron satiri iki gundur silinmisti ve kimse fark
# etmedi, cunku sessizlik hem "her sey yolunda" hem "hic calismiyor" demek
# oluyordu. Damga kilitten ONCE yazilir ki, kilit tutulu olsa bile "cron
# tetiklendi" bilgisi kaydedilsin — yoksa iki ayri ariza ayni sessizligi uretir.
date -u '+%Y-%m-%dT%H:%M:%S+00:00' > "${PROJE}/logs/.son-kosu"

# KONTROL DAMGASI — kalp atisinin yapamadigi seyi yapar.
# .son-kosu yalnizca "cron TETIKLEDI" der. Bu damga "kontrol TAMAMLANDI" der ve
# neyi neyle karsilastirdigini yazar. 2026-09-15 dersi: kalp atisi bes gun boyunca
# taptazeydi, panel "cron: saglikli" diyordu, ama betik kilitte takilip her turda
# sessizce cikiyordu. Tek damgayla "tetiklendi" ile "isini yapti" ayni goruntuyu
# uretiyordu; ikinci damga ikisini birbirinden ayirir.
kontrol_damgasi() {   # $1=yerel sha  $2=uzak sha  $3=sonuc etiketi
  printf '%s %s %s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')" "$1" "$2" "$3" \
    > "${PROJE}/logs/.son-kontrol"
}

# ---------------------------------------------------------------------- KILIT
# Ayni anda iki guncelleme calismasin (cron ust uste binebilir).
#
# 2026-09-15 ARIZASI — bu blogun varlik sebebi:
#   09-10'daki basarili dagitimdan sonra fd 9 uzun omurlu bir alt surece sizdi.
#   flock acik dosya TANIMINA baglidir; tanitici yasadikca kilit tutulu kalir.
#   Sonuc: BES GUN boyunca her cron turu kalp atisini yazdi, sonra `flock -n 9 ||
#   exit 0` ile SESSIZCE cikti. git fetch hic calismadigi icin origin/main de
#   bayat kaldi; `git status` "geride degilsin" dedi, panel "cron: saglikli" dedi.
#   Uc gosterge de yesildi, dongu oluydu.
#
# IKI DUZELTME:
#   1) Kilidi alan surecin PID'i yan dosyaya yazilir. Kilit alinamazsa sahibine
#      bakilir: sahip OLU ya da bu betigin bir ornegi DEGILSE kilit sizmistir.
#   2) Sizmis kilit KURTARILABILIR: flock dosyanin INODE'una baglidir, adina
#      degil. Dosyayi silip yeniden acmak yeni bir inode verir; sizdiran surec
#      eski inode'u tutmaya devam eder ama kimseyi engellemez.
# Ve en onemlisi: bu yoldan cikis artik ASLA sessiz degil.
KILIT="${PROJE}/logs/.guncelle.lock"
KILIT_PID="${PROJE}/logs/.guncelle.lock.pid"

kilit_al() {
  exec 9>"$KILIT"
  if flock -n 9; then echo "$$" > "$KILIT_PID"; return 0; fi

  local sahip; sahip=$(cat "$KILIT_PID" 2>/dev/null || true)
  # Sahip gercekten calisan bir guncelleme mi? /proc/PID/cmdline NUL ayracli,
  # o yuzden grep -a ile ham okuyoruz.
  if [ -n "$sahip" ] && kill -0 "$sahip" 2>/dev/null \
     && grep -qa "sunucu-otomatik-guncelle" "/proc/${sahip}/cmdline" 2>/dev/null; then
    kayit "Kilit ${sahip} nolu canli guncellemede — bu tur atlandi (normal)"
    return 1
  fi

  kayit "!! KILIT SIZMIS — sahip='${sahip:-bilinmiyor}' canli bir guncelleme degil."
  kayit "   Kilit dosyasi yenileniyor (yeni inode); sizan tanitici artik engellemeyecek."
  rm -f "$KILIT"
  exec 9>"$KILIT"
  if ! flock -n 9; then
    kayit "!! Kilit yenilendi ama yine alinamadi — tur atlandi, sebep bilinmiyor"
    return 1
  fi
  echo "$$" > "$KILIT_PID"
  BILDIR_MESAJ="🔓 Otomatik guncelleme KILITTE TAKILMISTI, kurtarildi.
Sizan bir dosya taniticisi kilidi tutuyordu; bu sure boyunca sunucu yeni kod
CEKMEDI ve bunu kimseye soylemedi. Kilit yenilendi, dongu devam ediyor." \
    BILDIR_TIP=send_error bildir
  kayit "   Kilit kurtarildi, guncellemeye devam ediliyor."
  return 0
}

kilit_al || exit 0

# ---------------------------------------------------------------- yeni kod var mi
# Damga YAZILMAZ: fetch patladiysa kontrol TAMAMLANMAMISTIR. Damgayi yine de
# yazsaydik "her sey yolunda" derdik — tam da kacindigimiz yalan bu.
git fetch origin "$DAL" --quiet 2>/dev/null || { kayit "fetch basarisiz (ag?)"; exit 0; }
YEREL=$(git rev-parse HEAD)
UZAK=$(git rev-parse "origin/${DAL}")
if [ "$YEREL" = "$UZAK" ]; then
  kontrol_damgasi "$YEREL" "$UZAK" guncel   # log'a yazmaz ama damgayi tazeler
  exit 0
fi

kayit "Yeni surum: ${YEREL:0:7} -> ${UZAK:0:7}"

# --------------------------------------------------- guncelleme ONCESI durum kaydi
# Acik pozisyonlarin "SEMBOL:giris" ozeti. Guncelleme sonrasi bu dizgi BIREBIR
# ayni cikmali — kullanicinin degismez sarti: "guncelleme pozisyonu kapatmasin".
pozisyon_ozeti() {
  curl -s --max-time 15 "${PANEL}/api/state" 2>/dev/null | "$PY" -c "$(cat <<'PYX'
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print("OKUNAMADI")
    sys.exit()
print(";".join(sorted(
    str(p.get("symbol")) + ":" + repr(p.get("entry"))
    for p in d.get("positions", []))))
PYX
)"
}

ONCE=$(pozisyon_ozeti)
kayit "Panel adresi: ${PANEL}"
kayit "Once acik pozisyonlar: ${ONCE:-yok}"

# SESSIZ ERTELEME OLMAZ. Ilk surumde panel adresi yanlisti; kapi her seferinde
# devreye girdi, guncelleme hic yapilmadi ve KIMSE HABERI OLMADI. Erteleme artik
# Telegram'a bildirilir — ama her 5 dakikada bir degil, sadece ilk seferinde
# (damga dosyasi). Panel geri gelince damga silinir.
DAMGA="${PROJE}/logs/.panel-erisilemedi"
if [ "$ONCE" = "OKUNAMADI" ]; then
  kayit "!! Panel okunamadi (${PANEL}) — pozisyon dogrulanamayacagi icin guncelleme ERTELENDI"
  if [ ! -f "$DAMGA" ]; then
    touch "$DAMGA"
    BILDIR_MESAJ="⚠️ Otomatik guncelleme ERTELENDI
Panel okunamiyor: ${PANEL}
Acik pozisyonlar dogrulanamadigi icin yeni kod uygulanmadi.
Bot eski surumde calismaya devam ediyor." BILDIR_TIP=send_error bildir
  fi
  kontrol_damgasi "$YEREL" "$UZAK" ertelendi-panel
  exit 0
fi
rm -f "$DAMGA"

# ------------------------------------------------------------- kodu al ve SINA
# Once gecici olarak al; testler gecmezse GERI DON.
git reset --hard "origin/${DAL}" --quiet || { kayit "HATA: reset basarisiz"; exit 1; }

if [ requirements.txt -nt .venv/.kurulum-damgasi ] 2>/dev/null; then
  kayit "requirements.txt degismis, bagimliliklar guncelleniyor"
  "$PY" -m pip install --quiet -r requirements.txt && touch .venv/.kurulum-damgasi
fi

kayit "Kritik testler calistiriliyor..."
if ! "$PY" -m tests.kritik_testler >> "$LOG" 2>&1; then
  kayit "!! TESTLER BASARISIZ — ${YEREL:0:7} surumune GERI DONULUYOR, bot yeniden baslatilmadi"
  git reset --hard "$YEREL" --quiet
  BILDIR_MESAJ="⛔️ Otomatik guncelleme DURDURULDU — ${UZAK:0:7} testleri gecemedi.
Bot eski surumde (${YEREL:0:7}) calismaya devam ediyor.
Detay: logs/otomatik-guncelle.log" BILDIR_TIP=send_error bildir
  kontrol_damgasi "$YEREL" "$UZAK" test-basarisiz
  exit 1
fi
kayit "Testler gecti."

# ------------------------------------------------------------------ yeniden baslat
kayit "Bot yeniden baslatiliyor..."
# 9>&- ZORUNLU — yoksa otomatik guncelleme KENDINI KILITLER (2026-09-09):
#   cron -> bu betik (fd 9'da flock tutuyor) -> kur-sunucu.sh -> paneli-baslat.sh
#          -> nohup python -m src.panel &
# `nohup ... &` acik dosya tanitilarini KAPATMAZ; panel fd 9'u miras alir ve
# flock acik dosya TANIMINA bagli oldugu icin panel yasadikca kilit tutulu kalir.
# Sonuc: ilk BASARILI dagitimdan sonra her cron turu `flock -n 9 || exit 0` ile
# sessizce cikar ve otomatik guncelleme olur. 9>&- yalnizca bu alt surec icin
# tanitiyi kapatir; bu betigin kendi kilidi bozulmaz.
bash deploy/kur-sunucu.sh >> "$LOG" 2>&1 9>&-

# ---------------------------------------------------------- pozisyonlari DOGRULA
sleep 12
SONRA=$(pozisyon_ozeti)
kayit "Sonra acik pozisyonlar: ${SONRA:-yok}"

if [ "$ONCE" = "$SONRA" ]; then
  DURUM="✅ Pozisyonlar birebir korundu"
  TIP="send"
else
  DURUM="⚠️ POZISYONLAR DEGISTI — once[${ONCE}] sonra[${SONRA}]"
  TIP="send_error"
  kayit "$DURUM"
fi

BILDIR_MESAJ="🚀 Otomatik guncelleme ${YEREL:0:7} → ${UZAK:0:7}
• Testler: gecti
• ${DURUM}" BILDIR_TIP="$TIP" bildir

# Artik yereldeki surum UZAK'a esit — damga bunu yazar ki panel "geride" alarmini
# dusursun. Damgadaki iki sha esitse dongu saglikli demektir.
kontrol_damgasi "$UZAK" "$UZAK" dagitildi
kayit "Tamamlandi."
