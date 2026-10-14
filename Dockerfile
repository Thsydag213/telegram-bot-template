FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Бот не слушает порты: он сам ходит в Telegram (long polling)
RUN useradd --create-home bot
USER bot

CMD ["python", "bot.py"]
