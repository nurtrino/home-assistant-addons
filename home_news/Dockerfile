ARG BUILD_FROM
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app/ /app/
COPY sources.yaml /app/sources.yaml

CMD ["python", "-m", "main"]
