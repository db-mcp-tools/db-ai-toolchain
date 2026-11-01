#!/usr/bin/env bash
# 启动一个用于端到端验证的 MySQL 8.0（Docker）。
# 注意：本机 3306 已被本地 MySQL 5.7 占用，故映射到宿主机 3307。
# 用法： bash scripts/up-mysql.sh
set -euo pipefail

NAME=dbcore-mysql
IMAGE=mysql:8.0

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "容器 $NAME 已存在，确保其运行..."
  docker start "$NAME" >/dev/null 2>&1 || true
else
  echo "创建并启动 $NAME (host 3307 -> container 3306)..."
  docker run -d --name "$NAME" \
    -e MYSQL_ROOT_PASSWORD=root -e MYSQL_DATABASE=test \
    -p 3307:3306 "$IMAGE" >/dev/null
fi

echo "等待 MySQL 就绪..."
for i in $(seq 1 40); do
  if docker exec "$NAME" mysqladmin ping -uroot -proot --silent 2>/dev/null; then
    echo "MySQL 就绪"; break
  fi
  sleep 2
done

echo
echo "连接串： mysql://root:root@127.0.0.1:3307/test"
echo "运行 e2e： DBT_TEST_MYSQL_URL=mysql://root:root@127.0.0.1:3307/test PYTHONPATH=packages python tests/e2e_mysql.py"
