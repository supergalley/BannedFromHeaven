FROM python:3.12-slim
WORKDIR /app
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir --upgrade pip
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY wsgi.py .
RUN mkdir -p /app/app /data
CMD ["gunicorn","--workers","2","--threads","4","--timeout","300","--graceful-timeout","300","--error-logfile","-","--log-level","warning","-b","0.0.0.0:8000","wsgi:app"]