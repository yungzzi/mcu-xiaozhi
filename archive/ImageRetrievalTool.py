# Coze运行时相关
from runtime import Args
from typing import Dict, Any
import traceback

# ========== 图片URL映射表 ==========
URL_MAP = {
    "stm32_min": "https://s3.bmp.ovh/2026/04/20/NvQWr6Mr.png",
    "51_min": "https://s3.bmp.ovh/2026/04/21/seiijFZG.png",
    "seg": "https://s3.bmp.ovh/2026/04/20/HojoE7ha.png",
    "i2c": "https://s3.bmp.ovh/2026/04/20/qOExyzgx.png",
    "spi": "https://s3.bmp.ovh/2026/04/20/wA1fEBwu.png",
    "uart": "https://s3.bmp.ovh/2026/04/20/HcYZASLh.png",
    "nrf24l01": "https://s3.bmp.ovh/2026/04/21/PJGo85Br.png",
    "ds18b20": "https://s3.bmp.ovh/2026/04/21/JbwOTteU.png",
    "reset_circuit": "https://s3.bmp.ovh/2026/04/21/0OkYPeh2.png",
    "ir_transmitter": "https://s3.bmp.ovh/2026/04/21/7CUEd8Sr.png",
    "ir_receiver": "https://s3.bmp.ovh/2026/04/21/gcIP1X8N.png",
    "crystal_oscillator": "https://s3.bmp.ovh/2026/04/21/xwMY4OaB.png",
    "keypad_matrix": "https://s3.bmp.ovh/2026/04/21/9n5jLRW2.png",
    "led_chaser": "https://s3.bmp.ovh/2026/04/21/lgF9KGcs.png"
}


# ========== 入口函数 ==========
def handler(args: Args) -> Dict[str, Any]:
    # logger 是 Coze 注入的日志记录器，用于在云端输出调试信息（类似于 print，但会持久化到平台日志）
    logger = args.logger
    logger.info(f"原始输入数据: {args.input}")

    try:
        # ----- 兼容多种输入类型 -----
        # args.input 可能是 dict，也可能是 Coze 的 CustomNamespace 对象
        raw_input = args.input
        if isinstance(raw_input, dict):
            input_dict = raw_input
        elif hasattr(raw_input, '__dict__'):
            input_dict = raw_input.__dict__   # 将 CustomNamespace 转为字典
        else:
            input_dict = {}

        # ----- 清洗输入值 -----
        # .lower() 统一小写，.strip() 去除首尾空格/换行，防止意外空白导致匹配失败
        circuit_type = input_dict.get("circuit_type", "").lower().strip()

        # 防御性判断：确保 logger 存在再调用，避免沙箱环境中 logger 为 None 导致崩溃
        if logger:
            logger.info(f"解析后 circuit_type: '{circuit_type}'")

        # ----- 空值处理 -----
        if not circuit_type:
            available = ", ".join(URL_MAP.keys())
            return {"message": f"请指定电路类型。目前支持：{available}"}

        # ----- 检索匹配 -----
        if circuit_type in URL_MAP:
            img_url = URL_MAP[circuit_type]
            markdown = f"![{circuit_type}电路图]({img_url})"   # Markdown 图片语法，Coze 对话会自动渲染
            logger.info(f"返回图片URL: {img_url}")
            return {
                "message": f"为您找到 {circuit_type} 电路图：\n\n{markdown}",
                "image_url": img_url
            }
        else:
            available = ", ".join(URL_MAP.keys())
            logger.warning(f"不支持的电路类型: {circuit_type}")
            return {"message": f"暂不支持该电路类型。当前支持：{available}"}

    except Exception as e:
        # traceback.format_exc() 获取完整的错误堆栈，便于云端排错
        error_detail = traceback.format_exc()
        logger.error(f"处理异常: {error_detail}")
        return {"message": f"插件内部错误：{str(e)}"}
