FROM python:3.12-slim

# Güvenlik: root olmayan kullanıcı
RUN useradd --create-home --uid 10001 botuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY backtest/ ./backtest/

# Veri klasörü (volume bağlanacak) ve sahiplik
RUN mkdir -p /app/data && chown -R botuser:botuser /app

USER botuser

EXPOSE 8484

# Panel ana süreçtir: BAŞLAT/DURDUR butonlarını sunar ve botu alt süreç olarak
# yönetir (watchdog düşerse geri getirir). Konteyner restart:always ile birlikte
# "başlat deyince durdur diyene kadar çalışsın" garantisini verir.
CMD ["python", "-u", "-m", "src.panel"]
