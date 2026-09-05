#!/usr/bin/env bash
# Sunucuda yonetici erisimi icin hangi yollar acik? (SADECE OKUMA - hicbir sey degistirmez)
# Kullanim (sunucuda):  bash ~/al-sat/deploy/sudo-kontrol.sh

echo "=================================================="
echo "  Sunucu yonetici erisim kontrolu"
echo "=================================================="
echo "kullanici : $(whoami)"
echo "makine    : $(hostname)"
echo "dagitim   : $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"
echo "cekirdek  : $(uname -r)"
echo

echo "--- 1) Sifresiz sudo hakki var mi? ---"
if sudo -n true 2>/dev/null; then
  echo "  EVET! Sifre gerekmiyor. Sunu calistirabilirsin:"
  echo "     sudo apt install -y python3-pip python3-venv"
else
  echo "  Hayir, sifre gerekiyor."
fi
echo

echo "--- 2) Hangi komutlar sifresiz calisabilir? ---"
sudo -n -l 2>/dev/null | sed 's/^/  /' || echo "  (liste alinamadi - sifre gerekiyor)"
echo

echo "--- 3) Sudo yetkisi olan kullanicilar ---"
getent group sudo 2>/dev/null | sed 's/^/  sudo grubu: /'
getent group wheel 2>/dev/null | sed 's/^/  wheel grubu: /'
getent group admin 2>/dev/null | sed 's/^/  admin grubu: /'
echo

echo "--- 4) Makine tipi (kurtarma yontemi icin) ---"
if [ -d /sys/firmware/efi ]; then echo "  UEFI sistem"; else echo "  BIOS sistem"; fi
if systemd-detect-virt -q 2>/dev/null; then
  echo "  SANAL MAKINE: $(systemd-detect-virt)"
  echo "  -> Kurtarma: hipervizor konsolundan (Proxmox/VirtualBox/VMware web arayuzu)"
else
  echo "  FIZIKSEL makine"
  echo "  -> Kurtarma: monitor+klavye baglayip GRUB menusunden"
fi
[ -f /boot/cmdline.txt ] && echo "  Raspberry Pi tespit edildi (SD kart yontemi gerekir)"
echo

echo "--- 5) Onyukleyici ---"
if [ -d /boot/grub ] || [ -d /boot/grub2 ]; then
  echo "  GRUB mevcut -> kurtarma modu kullanilabilir"
else
  echo "  GRUB bulunamadi"
fi
echo

echo "--- 6) Python durumu (sudo gerekmeden kurulum mumkun mu?) ---"
echo "  python3 : $(python3 --version 2>&1)"
if python3 -m pip --version >/dev/null 2>&1; then
  echo "  pip     : VAR -> $(python3 -m pip --version)"
  echo "  ** SUDO'YA GEREK YOK, kurulum calisabilir! **"
else
  echo "  pip     : YOK"
  echo "  -> get-pip ile kullanici hesabina kurulabilir (sudo gerekmez)"
fi
python3 -c "import ensurepip" 2>/dev/null && echo "  ensurepip: VAR (venv kurulabilir)" || echo "  ensurepip: yok"
echo "=================================================="
