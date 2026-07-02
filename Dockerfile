FROM python:3.14-slim

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/main.py .

RUN useradd -u 10001 -m ota
USER ota

EXPOSE 8080
VOLUME ["/builds"]

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
