FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt -c requirements.lock
COPY main.py model-artifacts.json ./
RUN mkdir /embedding-cache && chown 65534 /embedding-cache
USER 65534
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
