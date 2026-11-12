# 阿里云生产部署方案（待用户 + 审阅 Agent 批准，未执行）

目标：在现有 HTTPS 入口下新增 `/mcu-xiaozhi/`，与 `/v360/` 并列，不影响在跑服务。
本文档基于 2026-11-12 只读勘察结果编写；批准前不做任何服务器变更。

## 1. 服务器现状（只读勘察）

- 主机 aliyun-dev（47.116.68.205），部署用户 yungzzi，**UID 1001**
- Docker 29.9.2 + Compose v5.6.1；**yungzzi 无 docker.sock 权限，需 sudo docker**
- nginx 1.24.0；80/443 监听。443 站点 `swim-kick-tracker`（IP 证书
  `/etc/letsencrypt-swim/live/swim-kick-tracker-ip/`），通过 include snippets 挂载子路径：
  - `v360-web.conf`：/v360 → 127.0.0.1:8080（容器 v360-web-v360-1，healthy，已运行 8 天）
  - `personal-proxy-center.conf`：/personal-proxy-center → 127.0.0.1:18787 + 静态文件
  - `personal-proxy-reference.conf`：/proxy-domains* 重定向
- 其他监听端口：18080（swim-kick-tracker）、7000、7443、16443、18787 等，均不触碰
- **8090 端口空闲**（本方案使用）
- 资源：磁盘 40G 用 50%；内存 1613MB 总量、约 900MB 可用（v360 限额 768m）
- `/srv/mcu-xiaozhi` 不存在；`/srv/v360-web` 为既有服务目录（不动）

## 2. 部署架构

```
https://47.116.68.205/mcu-xiaozhi/
  → nginx 443 站点 include snippets/mcu-xiaozhi.conf
  → proxy_pass http://127.0.0.1:8090/（剥前缀）
  → 容器 mcu-xiaozhi（镜像 mcu-xiaozhi:0.1.1，UID 1000，只读根文件系统）
     持久卷：/srv/mcu-xiaozhi/{data,uploads}
     secrets：/srv/mcu-xiaozhi/secrets/{coze_token.txt,mcu_password.txt} → /run/secrets/
```

与 v360-web 完全同构（版本化镜像、127.0.0.1 绑定、secret 只读挂载、健康检查）。

## 3. 执行步骤（批准后按序执行）

1. 本地构建并导出镜像（WSL 已构建 mcu-xiaozhi:0.1.1）：
   `docker save mcu-xiaozhi:0.1.1 | gzip > mcu-xiaozhi-0.1.1.tar.gz`，scp 到服务器后
   `sudo docker load`。（服务器无仓库拉取依赖；也可批准后在服务器构建）
2. 建目录与凭证（**UID 1000 是容器运行用户，与部署用户 1001 不同，必须 chown**）：
   ```bash
   sudo mkdir -p /srv/mcu-xiaozhi/{data,uploads,secrets}
   # 用户写入 coze_token.txt（SAT）与 mcu_password.txt（初始共享密码）后：
   sudo chown -R 1000:1000 /srv/mcu-xiaozhi/data /srv/mcu-xiaozhi/uploads
   sudo chown 1000:1000 /srv/mcu-xiaozhi/secrets/*.txt
   sudo chmod 0400 /srv/mcu-xiaozhi/secrets/*.txt
   sudo chmod 0700 /srv/mcu-xiaozhi/secrets
   ```
3. 放置 compose.yaml 与 .env（来自仓库 config.example.env，改
   `MCU_HOME=/srv/mcu-xiaozhi`、`MCU_SECRET_DIR=/srv/mcu-xiaozhi/secrets`、
   `COZE_MODE=real`、`COZE_BOT_ID=7615504548140466203`、`MCU_PORT=8090`）。
4. `sudo docker compose up -d`，等待 healthy。
5. nginx：新增 snippet（下节差异），`sudo nginx -t` 通过后 `sudo systemctl reload nginx`。
6. 验收清单（下节）。

## 4. nginx 配置差异（唯一改动点）

新增文件 `/etc/nginx/snippets/mcu-xiaozhi.conf`：

```nginx
location /mcu-xiaozhi/ {
    proxy_pass http://127.0.0.1:8090/;
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

在 `/etc/nginx/sites-enabled/swim-kick-tracker` 的 443 server 块内、
`include .../v360-web.conf;` 旁新增一行：

```nginx
    include /etc/nginx/snippets/mcu-xiaozhi.conf;
```

不修改任何既有 location / snippet / 证书配置。

## 5. 回滚办法

- 应用回滚：`.env` 的 MCU_IMAGE 改回上一版 tag → `sudo docker compose up -d`（数据卷不动）
- 完全下线：删除 include 行 + `sudo nginx -t && sudo systemctl reload nginx` →
  `sudo docker compose down`（保留 /srv/mcu-xiaozhi 数据）
- nginx 任何异常：`sudo nginx -t` 会在 reload 前拦截错误配置

## 6. 风险与对策

| 风险 | 对策 |
| --- | --- |
| 内存紧张（可用约 900MB） | 容器限额 512m、cpus 1.0；上线后观察 `docker stats`，必要时降到 384m |
| UID 不一致读不到 secret | 第 2 步 chown 1000 + 0400；启动后容器内验证可读 |
| 影响在跑服务 | 只新增 snippet + include 一行；8090 空闲；不动 v360/swim/proxy 配置；reload 前 nginx -t |
| SSE 被缓冲 | proxy_buffering off（已本地剥前缀代理验证） |
| 镜像来源 | docker save/load 离线导入，不依赖服务器外网拉取 |

## 7. 上线验收清单

- [ ] 容器 healthy；容器内可读 /run/secrets/coze_token
- [ ] https://47.116.68.205/mcu-xiaozhi/login 可访问，登录跳转 /mcu-xiaozhi/chat
- [ ] 静态文件 200；逐段回复可见（SSE）
- [ ] 上传一张图片并图文问答（真实扣子，计入联调预算另行确认）
- [ ] 历史图片仅本会话可见；退出后旧附件 URL 404
- [ ] /v360/ 照常访问；swim-kick-tracker 与 personal-proxy-center 不受影响
- [ ] 重启容器后历史与图片仍在