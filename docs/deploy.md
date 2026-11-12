# 生产部署与回滚（上海阿里云，/mcu-xiaozhi/ 与 /v360/ 并列）

> 生产改动前：先给用户看配置差异与回滚办法，确认后再执行。

## 服务器目录

```
/srv/mcu-xiaozhi/
  data/       # SQLite（持久）
  uploads/    # 用户上传原图（持久）
  secrets/    # 受限凭证文件，chmod 600，属主为部署用户
    coze_token.txt    # 扣子 PAT（用户写入）
    mcu_password.txt  # 站内共享初始密码（用户写入）
```

secrets 经 Compose secret 只读挂载到容器 `/run/secrets/`，不进镜像与环境变量明文。

## 构建与版本

```bash
docker build -t mcu-xiaozhi:0.1.0 .
```

镜像必须带版本号；保留上一版 tag 用于回滚。

## 启动

```bash
cp config.example.env .env   # 按实际修改；COZE_MODE 联调后改 real
docker compose up -d
docker compose ps            # healthy 为准
```

容器只监听 127.0.0.1:8090（可配），由现有 HTTPS 入口反代。

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