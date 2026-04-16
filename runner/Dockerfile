FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd -m -u 10001 runner
WORKDIR /runner

RUN pip install --no-cache-dir \
    numpy==2.4.4 \
    pandas==3.0.2 \
    matplotlib==3.10.8 \
    scipy==1.17.1 \
    scikit-learn==1.8.0

COPY execute_python_code.py /runner/execute_python_code.py

USER runner

ENTRYPOINT ["python"]
