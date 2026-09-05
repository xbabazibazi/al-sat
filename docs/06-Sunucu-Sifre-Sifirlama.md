# Sunucu sudo şifresi sıfırlama (quonsoftware)

**Tespit edilen ortam:** Ubuntu 26.04 LTS · kernel 7.0.0-30 · UEFI · fiziksel makine ·
GRUB mevcut · kullanıcı `quon` sudo grubunda (yetki var, şifre unutulmuş).

> Uzaktan (SSH ile) yapılamaz — bu bir güvenlik özelliğidir. Makinenin
> **monitör + klavyesine** ihtiyacın var.

---

## Yöntem 1 — Ubuntu Kurtarma Modu (önerilen, en temiz)

1. Sunucuya monitör ve klavye bağla, `sudo reboot` yerine güç düğmesiyle veya
   SSH'tan `reboot` ile yeniden başlat.
2. Açılışta **GRUB menüsünü** yakala:
   - UEFI sistemlerde açılır açılmaz **Esc** tuşuna arka arkaya bas
   - (Menü otomatik geçiyorsa **Shift** tuşunu basılı tut)
3. **"Advanced options for Ubuntu"** → Enter
4. Sonunda **`(recovery mode)`** yazan çekirdeği seç → Enter
5. Kurtarma menüsünde **`root — Drop to root shell prompt`** → Enter
6. Dosya sistemi salt-okunur açılır, yazılabilir yap:
   ```
   mount -o remount,rw /
   ```
7. Şifreyi değiştir:
   ```
   passwd quon
   ```
   Yeni şifreyi iki kez gir (ekranda görünmez, normaldir).
8. Çık ve normal başlat:
   ```
   exit
   ```
   Menüden **`resume — Resume normal boot`** seç.

---

## Yöntem 2 — GRUB parametresi (kurtarma modu çalışmazsa)

1. GRUB menüsünde normal Ubuntu satırı seçiliyken **`e`** tuşuna bas
2. `linux /boot/vmlinuz...` ile başlayan satırı bul, **sonuna** ekle:
   ```
   init=/bin/bash
   ```
   (Aynı satırda `ro` varsa `rw` yap — işi kolaylaştırır)
3. **Ctrl + X** ile başlat
4. Kabuk gelince:
   ```
   mount -o remount,rw /
   passwd quon
   sync
   exec /sbin/init
   ```

---

## Dikkat edilecekler

- **Disk şifreliyse (LUKS):** açılışta disk parolası sorulur; onu bilmen gerekir.
  Bu, kullanıcı şifresinden farklıdır.
- **Secure Boot:** GRUB parametresi eklemeyi engellemez, sorun çıkarmaz.
- **BIOS şifresi varsa:** GRUB menüsüne girmeden önce o sorulabilir.
- Yeni şifreyi bu sefer **Vaultwarden'a kaydet** (zaten o sunucuda çalışıyor).

---

## Şifre sıfırlandıktan sonra

Botu sunucuya taşımak için tek komut:

```powershell
cd "D:\Projeler2\AL SAT BOT"
.\deploy\SUNUCUYA-KUR.ps1
```

İstersen önce eksik paketleri kurup işi garantiye alabilirsin:

```bash
sudo apt install -y python3-pip python3-venv
```

---

## Not: bot için şifre şart değil

`pip` sunucuda yok ama kurulum betiği bunu **sudo'suz** çözebiliyor
(`get-pip.py` ile kullanıcı hesabına kurulum). Yani şifre sıfırlamayı
beklemeden `.\deploy\SUNUCUYA-KUR.ps1` denenebilir.
