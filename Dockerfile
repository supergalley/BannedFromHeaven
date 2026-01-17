FROM python:3.12-slim

WORKDIR /app

# System deps if needed later; for now keep it lean
RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN apt-get update && apt-get install -y nano && apt-get clean
RUN apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
COPY . .

# Create folder for SQLite DB
RUN mkdir -p /data
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1
CMD ["gunicorn","--workers", "2","--threads", "4","--timeout", "300","--graceful-timeout", "300","--error-logfile", "-","--log-level", "warning","-b", "0.0.0.0:8000","wsgi:app"]
