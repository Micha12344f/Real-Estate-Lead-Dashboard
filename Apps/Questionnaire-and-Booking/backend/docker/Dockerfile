ARG PYTHON_BASE_IMAGE=python:3.12-slim
FROM ${PYTHON_BASE_IMAGE}

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/app/
COPY docker/init/ /app/migrations/

# This image uses the base image's UID 0 (no USER override). Docker copies this
# private directory's ownership/mode into the new worker-only named volume.
RUN install -d -m 0700 -o 0 -g 0 /var/lib/tve-imap

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
