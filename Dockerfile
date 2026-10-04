FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/bot_constructor.db

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && adduser --disabled-password --gecos "" --uid 10001 botapp \
    && mkdir -p /data \
    && chown botapp:botapp /data

COPY config.py database.py main.py main_bot.py sub_bot_manager.py ./
COPY services ./services

USER botapp
CMD ["python", "main.py"]
