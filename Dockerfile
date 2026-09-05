FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# -u: loglar gecikmesiz olarak `docker logs` çıktısına düşer
CMD ["python", "-u", "-m", "src.main"]
