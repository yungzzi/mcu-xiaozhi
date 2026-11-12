#!/bin/sh
set -e
exec python -m uvicorn app.main:create_app --factory \
  --host 0.0.0.0 --port 8080 \
  ${MCU_ROOT_PATH:+--root-path "$MCU_ROOT_PATH"}