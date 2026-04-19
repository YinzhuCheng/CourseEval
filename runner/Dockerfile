FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd -m -u 10001 runner
WORKDIR /runner

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /runner/requirements.txt
RUN pip install --no-cache-dir -r /runner/requirements.txt

COPY execute_code.py /runner/execute_code.py

USER runner

ENTRYPOINT ["python"]
