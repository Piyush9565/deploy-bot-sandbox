FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .

# Same non-root user the Kubernetes securityContext expects.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin bot
USER 10001

CMD ["python", "/app/bot.py"]
