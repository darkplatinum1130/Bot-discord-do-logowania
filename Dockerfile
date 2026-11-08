FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/data/data.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
RUN useradd --create-home bot && mkdir /data && chown bot /data
USER bot

VOLUME /data
EXPOSE 8080
CMD ["python", "bot.py"]
