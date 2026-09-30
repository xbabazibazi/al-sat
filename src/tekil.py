"""TEK ÖRNEK KİLİDİ — aynı anda iki bot çalışmasını İMKÂNSIZ kılar.

NEDEN YAZILDI (2026-09-30): sunucuda SEKİZ GÜN boyunca iki bot birden
çalıştı (pid 8986 ve 18256, 22 Eylül'de 11 dakika arayla doğmuşlar).
İkisi de aynı SQLite'a yazdı, aynı log dosyasını döndürdü ve aynı Telegram
kuyruğunu çekti — Telegram saniyede bir "Conflict: terminated by other
getUpdates request" döndürdü. Kimse fark etmedi; kullanıcı Telegram'daki
hata seline kadar bilmiyordu.

ESKİ KORUMA NEDEN TUTMADI: main.py açılışta `bot_heartbeat` damgasının
yaşına bakıp 120 saniyeden tazeyse çıkıyordu. İki kusuru vardı:
  1) YARIŞ: bakmak ile `bot_pid`'i yazmak ATOMİK DEĞİL. İki süreç aynı
     anda "kimse yok" görüp ikisi de devam edebilir.
  2) YALNIZCA AÇILIŞTA: bir kez ikisi de geçtiyse, bir daha hiç
     bakılmıyor. Kalp atışı bir ağ takılmasında 120 sn bayatlarsa
     (10 parite × poll + uyku) nöbetçi ikinciyi başlatıyor ve o ikili
     sonsuza kadar yaşıyor.

ÇÖZÜM: işletim sisteminin kilidi. Atomik, bayatlamaz, süreç ölünce
çekirdek kilidi KENDİLİĞİNDEN bırakır — "kilit dosyası kalmış" sorunu yok.

KİLİT SÜREÇ ÖMRÜ BOYUNCA TUTULUR: dönen tanıtıcı canlı tutulmalı, yoksa
çöp toplayıcı dosyayı kapatır ve kilit düşer. Ayrıca tanıtıcı çocuk
süreçlere MİRAS BIRAKILMAZ (close_fd/FD_CLOEXEC) — bu projede tam olarak
o sızıntı yaşandı: dağıtım betiğinin flock'u miras alınıp beş gün boyunca
tutulu kaldı ve güncellemeler sessizce durdu.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


class KilitTutulu(Exception):
    """Başka bir örnek çalışıyor — bu süreç başlamamalı."""


class TekOrnekKilidi:
    """Süreç ömrü boyunca tutulan, platformdan bağımsız tek örnek kilidi."""

    def __init__(self, yol: Path) -> None:
        self.yol = Path(yol)
        self._fh = None

    @property
    def _pid_yolu(self) -> Path:
        """Sahibin pid'i AYRI dosyada tutulur.

        Kilit dosyasına yazmak cazip ama Windows'ta yanlış: msvcrt kilidi
        bayt aralığına konur ve o aralığı truncate/write etmek — kilidi
        tutan süreç için bile — PermissionError verir. Kilit dosyası saf
        kilit olarak kalsın; teşhis bilgisi yanında dursun.
        """
        return self.yol.with_suffix(self.yol.suffix + ".pid")

    def al(self) -> None:
        """Kilidi alır. Başkası tutuyorsa KilitTutulu fırlatır."""
        self.yol.parent.mkdir(parents=True, exist_ok=True)
        # "a+" : varsa dokunma, yoksa yarat. "w" olsaydı kilidi ALAMADAN
        # dosyayı sıfırlardık.
        self._fh = open(self.yol, "a+", encoding="utf-8")
        try:
            if sys.platform == "win32":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Çocuk süreçler bu tanıtıcıyı MİRAS ALMASIN. Alırlarsa
                # kilit, biz ölsek bile onların ömrü boyunca tutulu kalır.
                os.set_inheritable(self._fh.fileno(), False)
        except OSError as e:
            sahip = self._sahip_oku()
            self._kapat()
            raise KilitTutulu(
                f"Kilit başkasında{f' (pid {sahip})' if sahip else ''}: {self.yol}"
            ) from e

        # Kim tuttu: yalnızca teşhis için. Kilidin kendisi bu yazıya
        # DAYANMAZ — dosya silinse/bozulsa bile çekirdek kilidi doğru çalışır.
        try:
            self._pid_yolu.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass

    def _sahip_oku(self) -> str:
        try:
            return self._pid_yolu.read_text(encoding="utf-8").strip()[:20]
        except OSError:
            return ""

    def _kapat(self) -> None:
        if self._fh is not None:
            try:
                # Windows'ta kilit, dosya kapanınca da düşer ama açıkça
                # bırakmak sıranın hemen devredilmesini garantiler.
                if sys.platform == "win32":
                    import msvcrt
                    try:
                        self._fh.seek(0)
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def birak(self) -> None:
        """Kilidi bırakır. Süreç ölünce çekirdek zaten bırakır; bu
        yalnızca düzenli kapanış için."""
        self._kapat()

    def __enter__(self):
        self.al()
        return self

    def __exit__(self, *_):
        self.birak()
        return False
