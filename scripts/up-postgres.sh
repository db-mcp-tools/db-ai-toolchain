#!/usr/bin/env bash
# 一键起「端到端验证用」PostgreSQL 17。
# 本机 5432 已被其他项目的 Postgres 占用，这里映射到宿主机 5433。
# 用法：bash scripts/up-postgres.sh
set -euo pipefail

CONTAINER=dbcore-postgres
HOST_PORT=5433
IMAGE=postgres:17

if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    echo "[up-postgres] 容器已在运行：$CONTAINER"
  else
    echo "[up-postgres] 启动已存在的容器：$CONTAINER"
    docker start "$CONTAINER"
  fi
else
  echo "[up-postgres] 新建并启动 $IMAGE @ 0.0.0.0:$HOST_PORT->5432 (db=test, user=postgres, pw=root)"
  docker run -d --name "$CONTAINER" \
    -e POSTGRES_PASSWORD=root -e POSTGRES_DB=test -e POSTGRES_USER=postgres \
    -p "$HOST_PORT:5432" "$IMAGE"
fi

echo "[up-postgres] 等待就绪..."
for i in $(seq 1 30); do
  if docker exec "$CONTAINER" pg_isready -U postgres -d test >/dev/null 2>&1; then
    echo "[up-postgres] 就绪，连接串：postgres://postgres:root@127.0.0.1:$HOST_PORT/test"
    exit 0
  fi
  sleep 2
done
echo "[up-postgres] 超时仍未就绪，请检查 docker logs $CONTAINER" >&2
exit 1
