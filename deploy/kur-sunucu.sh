#!/usr/bin/env bash
# AL-SAT BOT — sunucuya SUDO'SUZ kurulum
#
# Neden sudo'suz: SSH ile komut çalıştırırken terminal olmadığı için sudo
# şifre soramıyor ("A terminal is required to authenticate"). Bu betik hiç
# sudo kullanmaz: sanal ortam yerine kullanıcı-yerel paketler, systemd yerine
# crontab (@reboot + 5 dakikalık nöbetçi) kullanır.
#
# Kullanım (sunucuda):  cd ~/al-sat && bash deploy/kur-sunucu.sh

set -uo pipefail
cd "$(dirname "$0")/.."
PROJE="$(pwd)"

echo "=================================================="
echo "  AL-SAT BOT — sudo'suz kurulum"
echo "  proje: $PROJE"
echo "=================================================="

# ---------- 1) Python bağımlılıkları (sudo YOK) ----------
echo "[1/5] Python bağımlılıkları kuruluyor..."
PY=""

# 1a) Sanal ortam denenir (varsa en temizi)
if python3 -m venv .venv >/dev/null 2>&1 && [ -x .venv/bin/pip ]; then
  if ./.venv/bin/pip install --quiet --upgrade pip >/dev/null 2>&1 \
     && ./.venv/bin/pip install --quiet -r requirements.txt; then
    PY="${PROJE}/.venv/bin/python"
    echo "      sanal ortam kullanılıyor"
  fi
fi

# 1b) venv yoksa: pip'i kullanıcıya kur (sudo gerekmez), sonra paketleri kur
if [ -z "$PY" ]; then
  rm -rf .venv 2>/dev/null
  echo "      sanal ortam yok -> kullanıcı-yerel kurulum deneniyor"

  if ! python3 -m pip --version >/dev/null 2>&1; then
    echo "      pip yok, kullanıcı için kuruluyor (get-pip)..."
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py \
      && python3 /tmp/get-pip.py --user 2>&1 | tail -n 3
    export PATH="$HOME/.local/bin:$PATH"
  fi

  if ! python3 -m pip --version >/dev/null 2>&1; then
    echo
    echo "HATA: pip kurulamadı. Sunucuda şunu bir kez çalıştırın:"
    echo "  sudo apt install -y python3-pip python3-venv"
    echo "(şifre sorulacak; sonra kurulum komutunu tekrar çalıştırın)"
    exit 1
  fi

  echo "      paketler kuruluyor..."
  if python3 -m pip install --user --quiet -r requirements.txt; then
    PY="python3"
  else
    echo "      (PEP 668 olabilir, --break-system-packages ile tekrar deneniyor)"
    if python3 -m pip install --user --break-system-packages --quiet -r requirements.txt; then
      PY="python3"
    else
      echo
      echo "HATA: bağımlılıklar kurulamadı. Yukarıdaki pip çıktısına bakın."
      exit 1
    fi
  fi
  echo "      sistem python + kullanıcı paketleri kullanılıyor"
fi

if ! "$PY" -c "import pandas, requests, dotenv" 2>&1; then
  echo "HATA: paketler içe aktarılamıyor (yukarıdaki mesaja bakın)."
  exit 1
fi
echo "      $($PY --version) hazır"

# ---------- 2) .env ----------
if [ ! -f .env ]; then
  echo "HATA: .env yok. Yerel makineden kopyalayın."
  exit 1
fi
chmod 600 .env
echo "[2/5] .env hazır (chmod 600)"

# ---------- 3) Tailscale IP + panel adresi ----------
TS_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
if [ -z "$TS_IP" ]; then
  TS_IP="$(ip -4 addr show tailscale0 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -n1 || true)"
fi
[ -z "$TS_IP" ] && TS_IP="100.85.134.94"

if grep -q "^PANEL_HOST=" .env; then
  sed -i -E "s#^PANEL_HOST=.*#PANEL_HOST=${TS_IP}#" .env
else
  echo "PANEL_HOST=${TS_IP}" >> .env
fi
mkdir -p data logs
echo "[3/5] Panel adresi: ${TS_IP}:8484"

# ---------- 4) Başlatıcı + nöbetçi ----------
cat > paneli-baslat.sh <<EOF
#!/usr/bin/env bash
# Panel zaten çalışıyorsa hiçbir şey yapma; değilse arka planda başlat.
cd "${PROJE}"
if curl -fsS "http://${TS_IP}:8484/api/state" >/dev/null 2>&1; then exit 0; fi
pkill -f "src.panel" >/dev/null 2>&1
sleep 1
nohup "${PY}" -u -m src.panel >> "${PROJE}/logs/panel.log" 2>&1 &
EOF
chmod +x paneli-baslat.sh

# crontab: acilista + her 5 dakikada bir nobetci (sudo gerekmez)
CRON_TMP="$(mktemp)"
crontab -l 2>/dev/null | grep -v "al-sat" > "$CRON_TMP" || true
echo "@reboot sleep 30 && ${PROJE}/paneli-baslat.sh   # al-sat" >> "$CRON_TMP"
echo "*/5 * * * * ${PROJE}/paneli-baslat.sh           # al-sat nöbetçi" >> "$CRON_TMP"
crontab "$CRON_TMP"
rm -f "$CRON_TMP"
echo "[4/5] Otomatik başlatma kuruldu (açılışta + 5 dakikalık nöbetçi)"

# ---------- 5) Şimdi başlat ----------
# GÜNCELLEME DURUMU: panel/bot zaten çalışıyorsa yeni kodu almaları için
# ikisini de yeniden başlatmak gerekir. Açık pozisyonlar SQLite'ta durduğu
# için kaybolmaz; bot açılışta kaldığı yerden devam eder.
if pgrep -f "src.panel" >/dev/null 2>&1 || pgrep -f "src.main" >/dev/null 2>&1; then
  echo "      mevcut süreçler yeni kod için yeniden başlatılıyor..."
  echo "      (açık pozisyonlar korunur — durum veritabanında)"
  pkill -f "src.main"  >/dev/null 2>&1
  pkill -f "src.panel" >/dev/null 2>&1
  sleep 3
  # Kalp atışını temizle: yoksa panel botu "hâlâ canlı" sanıp yenisini başlatmaz
  "$PY" - <<'PYEOF' 2>/dev/null || true
import sqlite3
c = sqlite3.connect("data/bot_state.db")
c.execute("UPDATE kv SET value='' WHERE key='bot_heartbeat'")
c.commit()
c.close()
PYEOF
fi

echo -n "[5/5] Panel başlatılıyor"
./paneli-baslat.sh
HAZIR=0
for i in $(seq 1 30); do
  if curl -fsS "http://${TS_IP}:8484/api/state" >/dev/null 2>&1; then
    HAZIR=1; echo " -> hazır"; break
  fi
  echo -n "."
  sleep 2
done

if [ "$HAZIR" -ne 1 ]; then
  echo " -> BAŞLAMADI"
  echo "--- panel logu ---"
  tail -n 30 logs/panel.log 2>/dev/null || echo "(log yok)"
  exit 1
fi

# Botu otomatik başlat
if curl -fsS -X POST "http://${TS_IP}:8484/api/start" >/dev/null 2>&1; then
  echo "      bot otomatik başlatıldı"
fi
sleep 6

echo
echo "--- Durum ---"
curl -fsS "http://${TS_IP}:8484/api/state" 2>/dev/null | head -c 300
echo
echo
echo "=================================================="
echo "  KURULUM TAMAM — BOT SUNUCUDA ÇALIŞIYOR"
echo "=================================================="
echo "  Panel : http://${TS_IP}:8484"
echo "  Log   : tail -f ~/al-sat/logs/panel.log"
echo "  Nöbetçi her 5 dakikada kontrol eder; sunucu"
echo "  yeniden başlarsa 30 saniye içinde panel açılır."
echo "=================================================="
