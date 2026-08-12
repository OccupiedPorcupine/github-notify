FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# uid is fixed so the bind-mounted ./data can be chowned to match on the host
RUN groupadd --gid 10001 bot \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin bot

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

USER bot

CMD ["python", "-m", "app.main"]
