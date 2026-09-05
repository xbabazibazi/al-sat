#!/usr/bin/env bash
# AL-SAT BOT — sunucuya güvenli container kurulumu
#
# Kullanım (sunucuda):
#   cd ~/al-sat && bash deploy/sunucuya-kur.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=================================================="
echo "  AL-SAT BOT — güvenli container kurulumu"
echo "=================================================="

# 1) Docker kontrolü
if ! command -v docker >/dev/null 2>&1; then
  echo "[1/5] Docker bulunamadı, kuruluyor..."
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER" || true
  echo "      NOT: Docker grubu için oturumu kapatıp açman gerekebilir."
else
  echo "[1/5] Docker mevcut: $(docker --version)"
fi

# 2) .env kontrolü (anahtarlar repoda YOK, elle kopyalanır)
if [ ! -f .env ]; then
  echo
  echo "HATA: .env dosyası yok. Yerel makinenden kopyala:"
  echo "  scp \"D:\\Projeler2\\AL SAT BOT\\.env\" $USER@quonsoftware:~/al-sat/.env"
  exit 1
fi
chmod 600 .env
echo "[2/5] .env bulundu ve izinleri kısıtlandı (chmod 600)"

# 3) Tailscale IP'sini bul ve porta kilitle
TS_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
if [ -n "$TS_IP" ]; then
  sed -i -E "s#\"[0-9.]+:8484:8484\"#\"${TS_IP}:8484:8484\"#" docker-compose.yml
  echo "[3/5] Panel portu Tailscale IP'sine kilitlendi: ${TS_IP}:8484"
else
  echo "[3/5] UYARI: tailscale IP okunamadı — docker-compose.yml içindeki IP'yi elle kontrol et!"
  TS_IP="<sunucu-ip>"
fi

# 4) Veri klasörü (konteyner root olmayan 10001 kullanıcısıyla yazar)
mkdir -p data
chown -R 10001:10001 data 2>/dev/null || sudo chown -R 10001:10001 data
echo "[4/5] Veri klasörü hazır"

# 5) Derle ve başlat
echo "[5/5] Konteyner derleniyor ve başlatılıyor..."
docker compose up -d --build
sleep 6
docker compose ps

echo
echo "=================================================="
echo "  KURULUM TAMAM"
echo "=================================================="
echo "  Panel : http://${TS_IP}:8484"
echo "  Log   : docker compose logs -f"
echo
echo "  Panelde ▶ BAŞLAT'a bas — bot çalışır ve sen DURDUR"
echo "  demedikçe durmaz. Sunucu yeniden başlasa bile"
echo "  konteyner + watchdog botu otomatik ayağa kaldırır."
echo
echo "  Güvenlik: root olmayan kullanıcı, salt-okunur dosya"
echo "  sistemi, tüm yetenekler düşürülmüş, port yalnızca"
echo "  Tailscale ağına açık."
echo "=================================================="
