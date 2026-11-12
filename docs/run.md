# 本地运行与测试

## 环境

- WSL Ubuntu，Python 3.14（仓库 `.venv`），Docker + Compose（容器验收用）
- 依赖锁定在 `requirements.lock.txt`；安装走阿里云 PyPI 镜像

## 本地开发（默认 mock，不触网、不耗扣子额度）

```bash
cd /home/yungzzi/workspace/04_tools-and-scripts/mcu-xiaozhi
python3 -m venv .venv
.venv/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.lock.txt

export MCU_DATA_DIR=/tmp/mcu-dev/data MCU_UPLOAD_DIR=/tmp/mcu-dev/uploads
export MCU_PASSWORD='dev-pass-123' MCU_SECURE_COOKIES=0 COZE_MODE=mock
.venv/bin/python -m uvicorn app.main:create_app --factory --port 8091
```

浏览器打开 http://127.0.0.1:8091/login ，账号 `mcu`，密码为 `MCU_PASSWORD`。

## 自动化测试

```bash
.venv/bin/python -m pytest tests/ -q
```

全部用例使用 mock 扣子客户端，不产生真实 API 调用。

## mock 触发词（仅 COZE_MODE=mock）

| 输入 | 行为 |
| --- | --- |
| `/mock quota` | 模拟额度耗尽错误 |
| `/mock auth` | 模拟凭证失效错误 |
| `/mock slow` | 慢速流（中断/单轮锁测试） |
| `/mock image` 或含「配图」 | 回复带站内样例配图 |
| `/mock brokenimage` | 回复带加载失败的图床 URL（降级为链接） |
| 含「代码」 | 回复带 Python 代码块 |
| 上传文件名含 `fail-upload` | 模拟扣子上传失败（502） |

## 真实模式（联调才用）

令牌使用扣子 **SAT**（用户已调整权限，需开通 `chat` 与 `uploadFile`）。

```bash
export COZE_MODE=real
export COZE_BOT_ID=<真实 bot_id>
export COZE_TOKEN_FILE=/path/to/restricted/token.txt   # 或 COZE_TOKEN 环境变量
```

真实令牌不写入仓库、镜像、前端与日志。遇鉴权/额度错误立即停止试请求。