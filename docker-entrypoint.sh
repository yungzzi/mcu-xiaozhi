#!/bin/sh
set -e
# 容器端口仅绑定宿主 127.0.0.1，只有本机 nginx 可达，故信任所有转发头来源；
# docker NAT 后源 IP 为网关地址，不能只信 127.0.0.1。
exec python -m uvicorn app.main:create_app --factory \
  --host 0.0.0.0 --port 8080 \
  --proxy-headers --forwarded-allow-ips "*" \
  ${MCU_ROOT_PATH:+--root-path "$MCU_ROOT_PATH"}
