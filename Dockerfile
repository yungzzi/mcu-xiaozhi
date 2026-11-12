FROM python:3.14-slim

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.lock.txt

COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && useradd --create-home --uid 1000 mcu \
    && mkdir -p /app/data /app/uploads \
    && chown -R mcu:mcu /app

USER mcu
EXPOSE 8080
ENTRYPOINT ["docker-entrypoint.sh"]