# mcu-xiaozhi

MCU 小智课程助手网页版：把运行在扣子（Coze 国内版）上的原智能体通过 OpenAPI 接入自建网站，
供老师和学生用手机浏览器登录使用。

## 首版范围

- FastAPI + Jinja2 + 少量 JavaScript + SQLite，单应用容器
- 共享站内账号登录；「记住登录」15 天 / 会话 Cookie 最长 12 小时
- 每个登录会话独立的聊天历史与附件，服务端逐次校验归属
- 扣子 `/v3/chat` 流式回复（stream=true, auto_save_history=true），conversation_id 复用
- 图片上传（JPG/PNG）→ 扣子 uploadFile → 图文问答；历史图片从本站读取
- 插件返回的图床 URL 安全渲染为课程配图，失败保留可点击链接
- 开发/测试默认使用本地模拟扣子接口，真实调用集中在联调阶段

## 状态

开发中。运行说明见 `docs/`（随功能分支补充）。

## 待补项

- [ ] 原智能体的两个旧 Python 文件（拿到后脱敏检查再归档到 `archive/`）
- [ ] 真实扣子 PAT 令牌与 bot_id（由用户写入服务器受限文件，经 Compose secret 挂载）
- [ ] 插件图床 URL 实际样例（联调时确认返回格式）