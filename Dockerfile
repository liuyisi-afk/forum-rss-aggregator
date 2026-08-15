FROM python:3.11-slim

WORKDIR /opt/rss-feed

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config ./config
COPY deploy ./deploy
COPY gunicorn.conf.py .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENV BIND_HOST=0.0.0.0 \
    PORT=28888 \
    RUN_MODE=app

EXPOSE 28888

ENTRYPOINT ["/opt/rss-feed/entrypoint.sh"]
