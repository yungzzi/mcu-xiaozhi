# 原扣子智能体归档

这里保存《单片机原理及应用》课程智能体的原始提示词、两个扣子插件工具的源码和配置截图。工具由扣子运行，本目录不参与 FastAPI 服务运行，也不进入 Docker 镜像。

- [提示词](prompt.md)
- [动态绘图源码](CircuitDrawTool.py) · [扣子元数据截图](CircuitDrawTool-metadata.png)
- [静态检索源码](ImageRetrievalTool.py) · [扣子元数据截图](ImageRetrievalTool-metadata.png)

## 插件 CircuitDiagramAssistant

截图中两个工具均已上线并启用。插件依赖为 matplotlib 3.10.8、requests 2.32.5、schemdraw 0.22。源码使用扣子注入的 runtime.Args。截图中另有未测试的 test_tool，本次仅归档两个正式工具。

| 工具 | 截图中的输入 | 截图中的输出 |
| --- | --- | --- |
| CircuitDrawTool | 必填 String circuit_type；可选 String r、c、r_base、diode、mcu_pin、use_rc、rc_r、rc_c、mcu_type | 必填 String message 和 image_url |
| ImageRetrievalTool | 必填 String circuit_type | 可选 String message |

CircuitDrawTool 的描述记录了 LED 电阻默认 220Ω、按键电阻默认 10kΩ、电容默认 0.1µF、基极电阻默认 1kΩ、电机续流二极管默认 1N5818。use_rc 为字符串，默认 false，启用时 rc_r 默认 10kΩ、rc_c 默认 0.1µF。截图中被截断的描述以平台原设置为准。绘图源码中的图床 UID 和 Token 已替换为占位符。

ImageRetrievalTool 的截图描述只列出六个关键词，源码 URL_MAP 实际包含 14 项。源码命中时还返回 image_url，但截图中仅声明了 message。这里保留两份原始记录，便于核对差异。

## 恢复范围

扣子的[查看智能体配置 API](https://docs.coze.cn/developer_guides_get_metadata_draft_published)可读取提示词、插件和工具清单等配置。[低代码工作流导出](https://docs.coze.cn/guides_import_and_export_workflow)是单独的功能，[复制或迁移资源](https://docs.coze.cn/guides_migrate_to_team)在扣子平台内进行。本归档供人工复建使用。目前没有归档实际模型配置、知识库源文件（51指令系统、CM3内核）或截图中未显示的参数描述，也未抓取真实智能体配置。
