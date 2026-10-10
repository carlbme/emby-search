FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin portal
WORKDIR /app
COPY portal/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY portal ./portal
USER 10001:10001
CMD ["python", "-m", "portal"]
