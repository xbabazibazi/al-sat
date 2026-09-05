#!/usr/bin/env bash
# AL-SAT BOT — sunucuya güvenli container kurulumu
#
# Kullanım (sunucuda):  cd ~/al-sat && bash deploy/sunucuya-kur.sh
#
set -uo pipefail
cd "$(dirname "$0")/.."

echo "=================================================="
echo "  AL-SAT BOT — güvenli container kurulumu"
echo "=================================================="

# ---------- 1) Docker ----------
if ! command -v docker >/dev/null 2>&1; then
  echo "[1/6] Docker yok, kuruluyor..."
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER" || true
else
  echo "[1/6] Docker mevcut: $(docker --version 2>/dev/null)"
fi

# Docker'a sudo'suz erişebiliyor muyuz? (yeni eklenen grup bu oturumda geçerli değil)
DC="docker compose"
if ! docker info >/dev/null 2>&1; then
  if sudo -n true 2>/dev/null || sudo true; then
    echo "      not: docker grubu bu oturumda aktif değil -> sudo kullanılacak"
    DC="sudo docker compose"
  else
    echo "HATA: docker'a erişilemiyor ve sudo yok."
    echo "      Çözüm: yeniden giriş yapın (logout/login) veya: newgrp docker"
    exit 1
  fi
fi

# ---------- 2) .env ----------
if [ ! -f .env ]; then
  echo "HATA: .env dosyası yok. Yerel makineden kopyalayın."
  exit 1
fi
chmod 600 .env
echo "[2/6] .env hazır (chmod 600)"

# ---------- 3) Tailscale IP ----------
TS_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
if [ -z "$TS_IP" ]; then
  TS_IP="$(ip -4 addr show tailscale0 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -n1 || true)"
fi
if [ -n "$TS_IP" ]; then
  sed -i -E "s#\"[0-9.]+:8484:8484\"#\"${TS_IP}:8484:8484\"#" docker-compose.yml
  echo "[3/6] Panel portu Tailscale IP'sine kilitlendi: ${TS_IP}:8484"
else
  TS_IP="100.85.134.94"
  echo "[3/6] UYARI: tailscale IP okunamadı, varsayılan kullanılıyor: $TS_IP"
fi

# ---------- 4) Derle ----------
echo "[4/6] Konteyner derleniyor..."
$DC down --remove-orphans >/dev/null 2>&1 || true
if ! $DC up -d --build; then
  echo
  echo "!! Konteyner başlatılamadı. Loglar:"
  $DC logs --tail 60
  exit 1
fi

# ---------- 5) Panel bekle ----------
echo -n "[5/6] Panel bekleniyor"
HAZIR=0
for i in $(seq 1 40); do
  if curl -fsS "http://${TS_IP}:8484/api/state" >/dev/null 2>&1 \
     || curl -fsS "http://127.0.0.1:8484/api/state" >/dev/null 2>&1; then
    HAZIR=1; echo " -> hazır"; break
  fi
  echo -n "."
  sleep 3
done

if [ "$HAZIR" -ne 1 ]; then
  echo " -> ZAMAN AŞIMI"
  echo
  echo "!! Panel ayağa kalkmadı. Tanı bilgileri:"
  echo "--- konteyner durumu ---"; $DC ps
  echo "--- son loglar ---";      $DC logs --tail 60
  echo "--- port dinleniyor mu ---"; (ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null) | grep 8484 || echo "8484 dinlenmiyor"
  exit 1
fi

# ---------- 6) Botu otomatik başlat ----------
if curl -fsS -X POST "http://${TS_IP}:8484/api/start" >/dev/null 2>&1 \
   || curl -fsS -X POST "http://127.0.0.1:8484/api/start" >/dev/null 2>&1; then
  echo "[6/6] Bot otomatik başlatıldı"
else
  echo "[6/6] UYARI: bot otomatik başlatılamadı — panelden BAŞLAT'a basın"
fi

sleep 6
echo
$DC ps
echo
echo "--- Bot durumu ---"
curl -fsS "http://${TS_IP}:8484/api/state" 2>/dev/null | head -c 400
echo
echo
echo "=================================================="
echo "  KURULUM TAMAM — BOT ÇALIŞIYOR"
echo "=================================================="
echo "  Panel : http://${TS_IP}:8484"
echo "  Log   : ${DC} logs -f    (klasör: ~/al-sat)"
echo
echo "  Bot zaten başlatıldı; sen DURDUR demedikçe durmaz."
echo "  Sunucu yeniden başlasa bile otomatik ayağa kalkar."
echo "=================================================="
