FROM python:3.12-slim

WORKDIR /code

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY notification_service ./notification_service
COPY main.py .
RUN useradd --uid 10001 --create-home service && mkdir /data && chown service:service /data
USER service
ENV AI_NOTIFY_DATABASE_PATH=/data/requests.sqlite3

EXPOSE 5000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5000"]
