#!/usr/bin/env bash
# AL-SAT BOT — sunucuya systemd ile kurulum (Docker YOK)
#
# Neden systemd: Docker'da kurulum/grup/volume izin sorunları çıkıyordu.
# systemd zaten sunucuda kurulu, sıfır bağımlılık, açılışta otomatik başlar,
# çökerse yeniden başlatır, logları journalctl'de tutar.
#
# Mimari:
#   systemd  ->  panel (her zaman ayakta)
#                  └── bot (panelin watchdog'u ayakta tutar; BAŞLAT/DURDUR ile yönetilir)
#
# Kullanım (sunucuda):  cd ~/al-sat && bash deploy/kur-systemd.sh

set -uo pipefail
cd "$(dirname "$0")/.."
PROJE="$(pwd)"
KULLANICI="$(whoami)"
SERVIS="alsat-panel"

echo "=================================================="
echo "  AL-SAT BOT — systemd kurulumu"
echo "  proje: $PROJE   kullanıcı: $KULLANICI"
echo "=================================================="

# ---------- 1) Python + venv ----------
echo "[1/6] Python ortamı hazırlanıyor..."
if ! command -v python3 >/dev/null 2>&1; then
  sudo apt-get update -qq && sudo apt-get install -y python3 python3-venv python3-pip
fi
if [ ! -d .venv ]; then
  python3 -m venv .venv 2>/dev/null || {
    sudo apt-get update -qq && sudo apt-get install -y python3-venv
    python3 -m venv .venv
  }
fi
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt
echo "      $(./.venv/bin/python --version) hazır"

# ---------- 2) .env ----------
if [ ! -f .env ]; then
  echo "HATA: .env yok. Yerel makineden kopyalayın:"
  echo "  scp \".env\" ${KULLANICI}@100.85.134.94:~/al-sat/.env"
  exit 1
fi
chmod 600 .env
echo "[2/6] .env hazır (chmod 600)"

# ---------- 3) Tailscale IP ----------
TS_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
if [ -z "$TS_IP" ]; then
  TS_IP="$(ip -4 addr show tailscale0 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -n1 || true)"
fi
[ -z "$TS_IP" ] && TS_IP="100.85.134.94"
echo "[3/6] Panel adresi: ${TS_IP}:8484"

# PANEL_HOST'u .env'e yaz (panel yalnızca Tailscale IP'sini dinlesin — internete kapalı)
if grep -q "^PANEL_HOST=" .env; then
  sed -i -E "s#^PANEL_HOST=.*#PANEL_HOST=${TS_IP}#" .env
else
  echo "PANEL_HOST=${TS_IP}" >> .env
fi

mkdir -p data

# ---------- 4) systemd servisi ----------
echo "[4/6] systemd servisi kuruluyor..."
sudo tee /etc/systemd/system/${SERVIS}.service >/dev/null <<EOF
[Unit]
Description=AL-SAT Bot Paneli (botu watchdog ile ayakta tutar)
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=${KULLANICI}
WorkingDirectory=${PROJE}
ExecStart=${PROJE}/.venv/bin/python -u -m src.panel
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# Güvenlik sıkılaştırması
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=${PROJE}/data ${PROJE}
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ${SERVIS} >/dev/null 2>&1
sudo systemctl restart ${SERVIS}

# ---------- 5) Panel bekle ----------
echo -n "[5/6] Panel bekleniyor"
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
  echo
  echo "--- servis durumu ---"
  sudo systemctl status ${SERVIS} --no-pager -l | head -n 20
  echo "--- son loglar ---"
  sudo journalctl -u ${SERVIS} -n 40 --no-pager
  exit 1
fi

# ---------- 6) Botu otomatik başlat ----------
if curl -fsS -X POST "http://${TS_IP}:8484/api/start" >/dev/null 2>&1; then
  echo "[6/6] Bot otomatik başlatıldı"
else
  echo "[6/6] UYARI: bot başlatılamadı — panelden BAŞLAT'a basın"
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
echo "  Durum : sudo systemctl status ${SERVIS}"
echo "  Log   : sudo journalctl -u ${SERVIS} -f"
echo
echo "  Sunucu yeniden başlasa bile panel otomatik açılır,"
echo "  bot da kaldığı yerden devam eder. PC'ni kapatabilirsin."
echo "=================================================="
