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
set -uo pipefail

PROJE="${HOME}/al-sat"
LOG="${PROJE}/logs/otomatik-guncelle.log"
DAL="${DEPLOY_DAL:-main}"
PANEL="http://127.0.0.1:8484"

mkdir -p "${PROJE}/logs"
kayit() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

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

# Ayni anda iki guncelleme calismasin (cron ust uste binebilir).
exec 9>"${PROJE}/logs/.guncelle.lock"
flock -n 9 || exit 0

# ---------------------------------------------------------------- yeni kod var mi
git fetch origin "$DAL" --quiet 2>/dev/null || { kayit "fetch basarisiz (ag?)"; exit 0; }
YEREL=$(git rev-parse HEAD)
UZAK=$(git rev-parse "origin/${DAL}")
[ "$YEREL" = "$UZAK" ] && exit 0        # degisiklik yok, sessizce cik

kayit "Yeni surum: ${YEREL:0:7} -> ${UZAK:0:7}"

PY="${PROJE}/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

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
kayit "Once acik pozisyonlar: ${ONCE:-yok}"
if [ "$ONCE" = "OKUNAMADI" ]; then
  kayit "!! Panel okunamadi — pozisyon dogrulamasi yapilamayacagi icin guncelleme ERTELENDI"
  exit 0
fi

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
  exit 1
fi
kayit "Testler gecti."

# ------------------------------------------------------------------ yeniden baslat
kayit "Bot yeniden baslatiliyor..."
bash deploy/kur-sunucu.sh >> "$LOG" 2>&1

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

kayit "Tamamlandi."
