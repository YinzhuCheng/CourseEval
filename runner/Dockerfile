FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd -m -u 10001 runner
WORKDIR /runner

RUN pip install --no-cache-dir \
    nbclient \
    nbconvert \
    nbformat \
    ipykernel \
    jupyter-client \
    numpy \
    pandas \
    matplotlib \
    scipy \
    scikit-learn && \
    python -m ipykernel install --sys-prefix --name python3

COPY execute_notebook.py /runner/execute_notebook.py
COPY execute_python_code.py /runner/execute_python_code.py

USER runner

ENTRYPOINT ["python", "/runner/execute_notebook.py"]
