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
- [x] 历史图片仅本会话可见；退出后旧附件 URL 404（本地浏览器验收已证；生产待图文联调复核）
- [x] /v360/ 照常访问；swim-kick-tracker 与 personal-proxy-center 不受影响
- [x] 重启容器后历史与图片仍在（同一会话 Cookie 跨重启验证）

## 8. 部署执行记录（2026-11-12，用户批准后执行）

- 镜像：mcu-xiaozhi:0.1.1 由 main d651e55 重建，docker save/load 离线导入（54.7MB）
- 目录：/srv/mcu-xiaozhi/{data,uploads,secrets}；凭证 install -o 1000 -g 1000 -m 0400，
  secrets 目录 0700；.env 0600（MCU_USERNAME=shaodadianxin，COZE_MODE=real）
- 容器：mcu-xiaozhi-mcu-xiaozhi-1 healthy；容器内确认两个 secret 可读
- nginx：备份 /home/yungzzi/mcu-staging/swim-kick-tracker.bak-mcu-xiaozhi；
  新增 snippets/mcu-xiaozhi.conf + 443 站点 include 一行（grep 计数=1）；
  nginx -t 通过后 systemctl reload
- 公网验收（https://47.116.68.205/mcu-xiaozhi/）：
  登录页 200 且静态 URL 带前缀；静态 css 200；正确凭证登录 303→/mcu-xiaozhi/chat 200；
  错误密码 401；healthz ok；建会话成功；同一 Cookie 跨容器重启历史仍在
- 在跑服务基线对比：/ 、/v360/、/swim-kick-tracker/、/personal-proxy-center/
  变更前后均 200；v360-web-v360-1 持续 healthy（Up 8 days）
- 回滚就绪：nginx 备份 + 上一版镜像 tag 流程见第 5 节
- 待办：经 nginx 的 SSE 逐段回复验证需 1 次真实扣子请求，按额度规则另行确认后执行；
  生产图文问答验收 likewise。建议上线后轮换共享密码（rotate_password.py）
## 9. 上线后热修记录（2026-11-12 晚）

用户浏览器实测发现两个入口问题，已修复并验证：

1. HTTP 访问 404：80 端口 default 站点无 /mcu-xiaozhi/ 路由。
   修复：default 站点新增两条 location，301 跳转 HTTPS（备份
   /home/yungzzi/mcu-staging/default.bak-mcu-xiaozhi；nginx -t 通过后 reload）。
2. 登录 POST 被降级为 GET 跳回 /login：后端未识别 X-Forwarded-Proto，
   模板生成 http 绝对地址的表单 action；http POST 遇 301 变 GET。
   修复分支 qwen/proxy-headers（ef2df71 + 96e1be5）：uvicorn 启用
   --proxy-headers；--forwarded-allow-ips 初设 127.0.0.1 无效（docker NAT
   源 IP 为网关），改 "*"（容器端口仅绑宿主 127.0.0.1，仅本机 nginx 可达）。
   镜像 0.1.3 部署，表单 action 变 https。

验证：公网浏览器全流程 HTTP→HTTPS→登录→chat 通过；已登录直访 /chat 不再跳回；
在跑服务基线全部 200 不变。回滚 tag：0.1.1 / 0.1.2 保留。
qwen/proxy-headers 待审阅批准后合入 main。

## 执行记录：2026-11-13 UI 版升级（0.1.3 → 0.2.1）

用户确认合并 + 更新生产 + 1 次流式验证请求后执行。

1. **合并**：qwen/ui-redesign（审阅通过 dedf960）--no-ff 合入 main = f918cac，
   树与 dedf960 一致，78 passed，已推送。
2. **0.2.0 回归与恢复**：0.2.0 从 main 构建，遗漏了尚未合入 main 的
   proxy-headers 热修（表单 action 退回 http，登录会失败）。发现后立即将
   qwen/proxy-headers（96e1be5，即 0.1.3 线上验证过的版本）--no-ff 补合入
   main = aa9f0fe，重建 **0.2.1** 部署恢复。该分支属补合入，请审阅 Agent 追认。
3. **备份**：升级前停容器并整目录备份
   `/srv/mcu-xiaozhi/data.bak-pre-0.2.0-20261113-151435`（含 sqlite3/wal/shm）。
4. **迁移核对**：旧 4 条聊天标题原样保留，auto_title 标志正确（均为默认「新对话」→1）。
5. **公网验证（0.2.1）**：登录 303、聊天页 200、会话隔离、重命名往返、
   首问自动命名全部通过；/ 、/v360/ 、/swim-kick-tracker/ 、/personal-proxy-center/ 全部 200。
6. **流式验证受阻于额度**：2 次真实请求（1 次验证 + 1 次经用户批准的诊断）均返回
   **quota 错误，扣子错误码 4028**（Insufficient coze credits balance，
   提示等待额度刷新或升级付费版）。错误事件经 nginx 即时到达浏览器，
   额度错误处理链路验证正确；**SSE 逐段输出本身尚未验证**，待额度恢复后补 1 次请求。
7. **回滚**：.env 改回 `MCU_IMAGE=mcu-xiaozhi:0.1.3` + `docker compose up -d`；
   数据回滚用上述备份目录。0.1.3 / 0.2.0 / 0.2.1 镜像均保留。

累计真实扣子请求：联调 4 次（2026-11-12）+ 本次 2 次 = 6 次。
是否订阅付费额度由用户决定；免费额度刷新后可补流式验证。
