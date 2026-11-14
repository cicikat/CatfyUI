# 导演工作台 V1

先在「Agent 模式」验证 OpenAI 兼容文本模型和可选视觉模型。参考图和画幅在「视频生成」设置。打开「导演工作台」，填写创作要求与目标片长，可选导入 CSV 或 XLSX。导入表的镜头默认锁定，导演保留原提示词、生成参数及参考图绑定。

- 关闭「自主执行」时，确认初版分镜以及后续导演修改后才继续。
- 打开「视觉审片」会向所选服务商发送候选、相邻镜头和成片的抽帧。决策日志记录候选、图片数量和抽帧时间。静帧无法充分评估连续运动、声音和完整节奏。
- 暂停会保留项目和正在执行的任务。需要取消时使用「停止并取消项目任务」。Wan 按原任务的 prompt ID 跟踪，正在执行的其他 ComfyUI 任务继续运行。
- 选用候选后可填写入点和出点，也可移动镜头。保存后点「合成预览」或「重新出片」。重新合成只读已有候选，沿用源音轨并给无音频片段补静音。
- 成片面板分别显示技术检查和内容审查。缺少视觉模型、证据不足、预算超限或检测到持续黑场等情况会进入「待人工确认」。
- 成片、预览、时间线和报告均可下载。含剪辑素材的项目包包含时间线引用的候选 MP4。可用 JSON 检查和迁移剪辑数据，工作台的当前项目持续从本机数据库读取。

## 数据和迁移

首次启动自动创建 `film_projects`、`film_events`、`film_shots`。项目、决策和历史分镜都在 `data/studio.sqlite`。同一 SQLite 事务提交生成任务和项目映射。待生成的候选使用原 `jobs` 队列。重启后如果已有 Wan 回执，继续跟踪原 prompt；结果未明的中断任务交给人工处理。旧 Agent 记录沿用原表。

产物位于 `projects/<project_id>/<assembly_id>/`。每次合成保留一个版本。正式产物为 `final.mp4`、`timeline.json`、`film-report.json` 和 `film-report.md`。预览使用 `preview.mp4`。时间线 schema 1 使用有序视频轨、稳定镜头 ID、任务 ID、相对素材路径、秒级入点和出点、目标片长、帧率、画幅、裁切策略与硬切转场。

升级前可退出工作台并备份 `data/studio.sqlite`、`projects/` 和 `jobs/`。回滚到旧版本时可以保留新增表和项目目录，之后升级仍可读取。

## 本地接口

- `GET /api/films` 列出项目，`GET /api/films/{id}` 查看当前项目，`?revision=N` 查看历史分镜。
- `POST /api/films/start` 创建项目。继续使用现有 Agent 的模型配置和素材上传接口。
- `POST /api/films/{id}/{action}` 支持 `approve`、`reject`、`pause`、`resume`、`stop`、`lock`、`edit`、`review`、`render` 和 `folder`。编辑携带计划版本和唯一的决策 ID。合成使用 `kind: preview|final`。
- `GET /media/films/{id}/{kind}/{filename}` 读取产物，`?download=1` 下载。
- `GET /api/films/{id}/package` 下载项目包，`?media=1` 包含剪辑素材。

## 离线验收

使用带相邻 `ffprobe.exe` 的 FFmpeg。可用 `CATFYUI_FFMPEG` 指向它，使用已安装项目依赖的 Python。

```powershell
python -m unittest -v test_film_director
node --check director.js
```

设置 `CATFYUI_TEST_EVIDENCE` 可保留六镜成片、裁切前成片、时间线、报告、事件和项目包。测试用本地模拟模型与生成器、真实 SQLite 和 FFmpeg。配置真实文本和视觉模型后，再用实际 H3 或 Wan 执行创意短片验收。V1 采用硬切，HUD、字幕、单独配音和配乐轨留待后续。
