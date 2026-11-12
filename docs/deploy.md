# 生产部署与回滚（上海阿里云，/mcu-xiaozhi/ 与 /v360/ 并列）

> 生产改动前：先给用户看配置差异与回滚办法，确认后再执行。

## 服务器目录

```
/srv/mcu-xiaozhi/
  data/       # SQLite（持久），属主 UID 1000
  uploads/    # 用户上传原图（持久），属主 UID 1000
  secrets/    # 受限凭证文件（见下方权限要求）
    coze_token.txt    # 扣子 SAT 令牌（用户写入，已开通 chat 与 uploadFile 权限）
    mcu_password.txt  # 站内共享初始密码（用户写入）
```

secrets 经 Compose secret 只读挂载到容器 `/run/secrets/`，不进镜像与环境变量明文。

### 凭证文件权限（必须，否则容器读不到）

容器以 **UID 1000 / GID 1000** 运行；文件型 Compose secret 沿用宿主文件的属主与权限。
部署用户 UID 与 1000 不同时，仅设 0600 会导致应用无法读取凭证。按如下设置：

```bash
sudo mkdir -p /srv/mcu-xiaozhi/{data,uploads,secrets}
# 用户写入两个凭证文件后：
sudo chown -R 1000:1000 /srv/mcu-xiaozhi/data /srv/mcu-xiaozhi/uploads
sudo chown 1000:1000 /srv/mcu-xiaozhi/secrets/coze_token.txt /srv/mcu-xiaozhi/secrets/mcu_password.txt
sudo chmod 0400 /srv/mcu-xiaozhi/secrets/coze_token.txt /srv/mcu-xiaozhi/secrets/mcu_password.txt
sudo chmod 0700 /srv/mcu-xiaozhi/secrets
```

`MCU_SECRET_DIR` 必须指向该目录（config.example.env 默认 `/srv/mcu-xiaozhi/secrets`）。

## 构建与版本

```bash
docker build -t mcu-xiaozhi:0.1.1 .
```

镜像必须带版本号；保留上一版 tag 用于回滚。

## 启动

```bash
cp config.example.env .env   # 按实际修改；COZE_MODE 联调后改 real
docker compose up -d
docker compose ps            # healthy 为准
```

容器只监听 127.0.0.1:8090（可配），由现有 HTTPS 入口反代。

## 共享密码轮换（运维操作）

站内不提供改密接口。轮换由部署管理员执行，轮换后所有设备旧登录立即失效：

```bash
docker compose exec mcu-xiaozhi python scripts/rotate_password.py
# 或从受限文件读取新密码：
docker compose exec mcu-xiaozhi python scripts/rotate_password.py --password-file /run/secrets/mcu_password
```

注意：`mcu_password.txt` 仅用于首次初始化；轮换后请同步更新该文件内容，
避免下次重建容器时用旧密码初始化新数据库（已有数据库不受影响）。

## Nginx 入口（在现有 server 块追加，不动 /v360/）

```nginx
location /mcu-xiaozhi/ {
    proxy_pass http://127.0.0.1:8090/;   # 尾斜杠剥前缀；应用以 --root-path 生成带前缀 URL
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Connection "";
    proxy_buffering off;          # SSE 逐段回复必需
    proxy_cache off;
    proxy_read_timeout 300s;
    client_max_body_size 10m;     # 图片上传
}
```

应用侧 `MCU_ROOT_PATH=/mcu-xiaozhi`（compose 默认），Cookie `Path=/mcu-xiaozhi`。

## 上线检查清单

- [ ] 登录跳转正确（/mcu-xiaozhi/login → /mcu-xiaozhi/chat）
- [ ] 静态文件（css/js/样例图）200
- [ ] 逐段回复可见（SSE 不被缓冲）
- [ ] 历史图片从本站加载且仅本会话可见
- [ ] /v360/ 照常访问
- [ ] 容器 healthy，重启后历史与图片仍在（持久卷）

## 回滚

```bash
# .env 中 MCU_IMAGE 改回上一版 tag
docker compose up -d
```

数据卷不动，回滚不丢历史与图片。若需撤销 nginx 变更，删除新增 location 块并 reload。