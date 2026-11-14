# CatfyUI

Windows 本地 AI 视频工作台。支持参考图、分镜表批量入队、视频画廊和 Agent 后台巡检。Agent 使用自填的 OpenAI 兼容 API 拆解指令，可选用视觉模型抽帧审查。视频由本机生成引擎执行。

## 启动

需要 Windows、Python 3.10+ 和 FFmpeg。安装应用依赖后，用当前 Python 启动。

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe launch.py
```

也可以双击「启动AI视频工作台.cmd」。默认地址为 `http://127.0.0.1:8788`。可在启动前用环境变量 `VIDEO_STUDIO_PORT` 更换端口。

## 本机引擎

| 环境变量 | 填写内容 |
| --- | --- |
| `CATFYUI_FFMPEG` | FFmpeg 可执行文件路径。已加入 PATH 时可省略 |
| `CATFYUI_H3_ROOT` | 已配置的 FreeVideo-H3 根目录。程序在运行时读取该安装下的 `machine.json` |
| `CATFYUI_COMFY_ROOT` | 已配置的 ComfyUI 根目录。Wan 适配使用本机 8188 端口 |
| `CATFYUI_COMFY_PYTHON` | ComfyUI Python 路径。默认使用 ComfyUI 根目录下的 `.venv\Scripts\python.exe` |

可将已有 H3 和 Wan 工作流分别放入本机的 `workflows/h3-source.json` 与 `workflows/wan-source.json`。程序也会尝试从引擎目录复制已知模板。Wan 适配基于 `wan22_5b_i2v_template.json` 的节点和模型文件名，其他图需要调整适配代码。缺少模型、工作流或 FFmpeg 时，页面会显示未就绪。

仓库提供应用源码与依赖列表。本机设置、工作流 JSON、模型权重、API Key、数据库、素材、产物和日志均保存在部署机器上。Agent 的 Key 由当前 Windows 用户加密。详见 [Agent 使用说明](Agent使用说明.md)。

导演工作台的使用、数据迁移与离线验收见 [Director 使用说明](Director使用说明.md)。
