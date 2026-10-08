# 课记迁移包：目标电脑部署合同

你是目标电脑上的 Codex。请在**此文件夹**内把应用安装、配置、启动到可用状态，并把一次短人工验收的操作步骤交给用户。不要只列计划：执行无风险的安装、静态检查、服务启动和页面检查；处理实际遇到的环境错误。目标电脑可能是 macOS、Windows 或 Linux，先检测系统，所有命令使用当前文件夹路径，不引用原电脑的绝对路径。

## 交付范围

- 已包含：本地网页（五门课程）、浏览器控制的麦克风连续采集、12 秒 / 0 overlap 分段、SiliconFlow Qwen/Qwen3-ASR-1.7B 原始转录、累积课堂摘要、本地 MP4/音频多文件导入、停止/暂停（麦克风）、本地保存/重命名记录。
- 不包含：教材 PDF、OCR、向量库、本地模型、Cleaner、历史录音/请求证据、已保存的用户记录、真实凭据。`references/` 只有课程清单和少量术语提示，不是教材全文。
- 云端依赖：一份由用户在目标电脑**亲自**填入的 SiliconFlow 中国站 API Key。ASR 和摘要调用的是远程服务；没有本地模型可下载。`SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1`、ASR模型 `Qwen/Qwen3-ASR-1.7B`、摘要模型 `Qwen/Qwen3.5-35B-A3B` 已预设。不要擅自改模型、摘要提示词、分段或合并策略。
- 配置和运行日志中不得输出 Key 或任何前后缀；不要把 `.env` 提交或放入分享包。不要上传 PDF、本地音视频原文件给其他服务；转录时仅音频片段发送至 SiliconFlow。

## 1. 安装环境（先完成，不调用麦克风和 API）

1. 检查本机 Python **3.11+**、[Node **22.12+**](https://nodejs.org/en/download)、npm、FFmpeg/ffprobe。macOS 建议使用当前 [python.org Python 安装器](https://www.python.org/downloads/macos/) 或带 Tcl/Tk 的 Python；该包的系统文件选择窗口依赖 `tkinter`。检查 `python3 -m tkinter`（Windows 用 `py -3 -m tkinter`）；此检查会弹出一个小测试窗口，关闭即可。macOS 的 Python.org 发行版附带 Tcl/Tk，详见其[说明](https://www.python.org/download/mac/tcltk/)。
2. FFmpeg/ffprobe 只用于导入音视频。在 macOS 可使用 `brew install ffmpeg`（若 Homebrew 已安装）；Linux 用系统包管理器（若 `sounddevice` 找不到 PortAudio，再安装本机的 PortAudio 运行库）；Windows 可按 [FFmpeg 下载页](https://ffmpeg.org/download.html) 安装并加入 PATH。执行 `ffmpeg -version` 和 `ffprobe -version`。不要安装或下载 OCR/ASR 本地模型。
3. macOS/Linux，在包根目录执行：
   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install --upgrade pip
   .venv/bin/python -m pip install -e .
   cd frontend && npm ci && npm run build && cd ..
   ```
   Windows PowerShell，在包根目录执行：
   ```powershell
   py -3 -m venv .venv
   & .\.venv\Scripts\python.exe -m pip install --upgrade pip
   & .\.venv\Scripts\python.exe -m pip install -e .
   Push-Location frontend; npm ci; npm run build; Pop-Location
   ```
   如 Python/Node 未安装，先安装本机对应架构的官方版本，再继续；不要复制旧电脑的 `.venv`、`node_modules` 或 `.heavy`。依赖版本在 `pyproject.toml`、`frontend/package-lock.json` 固定。
4. 执行 `.venv/bin/python verify_bundle.py`（Windows 可用 `.venv\Scripts\python.exe verify_bundle.py`）。该脚本只校验随包源码/配置，不访问麦克风或网络。随后执行 `.venv/bin/python -m unittest discover -s tests -v`（Windows 对应 `.venv\Scripts\python.exe`）；测试使用模拟设备与本机临时 HTTP 端口。

## 2. 本机配置与静态预检

1. 从 `.env.example` 复制到根目录 `.env`；**只由用户本人**在本机填写 `SILICONFLOW_API_KEY=`，不要让用户把 Key 发进聊天。保留其他默认值；`.env` 已在 `.gitignore` 中。
2. 用新虚拟环境运行 `python -m lecture_asr doctor --require-key`（Windows 改用 `.venv\Scripts\python.exe`）。输出只能展示 `api_key_status: configured` 等非敏感字段；若缺 Key，应等待用户本地填完再继续。再以只读方式检查 LLM 配置：
   ```sh
   .venv/bin/python -c "from lecture_asr.llm import load_llm_config; print(load_llm_config().safe_report())"
   ```
3. 运行 `.venv/bin/python -m lecture_asr list-input-devices`（Windows 对应 `.venv\Scripts\python.exe`），列出输入设备和系统默认项。包默认选**目标电脑的系统默认输入设备**；如用户要其他设备，在 `.env` 填 `LECTURE_INPUT_DEVICE=<显示的数字 index>`，重启后端。程序会固定此 index、名称和 host API，绝不静默切换设备。仅检查设备元数据，不打开录音流。
4. 本应用采集为 **44100 Hz、mono、PCM16**，不在麦克风路径上重采样。执行 `.venv/bin/python check_device.py`（Windows 用 `.venv\Scripts\python.exe check_device.py`），实际调用 PortAudio 的 `check_input_settings`，但不打开流。如果不支持，报告具体错误并在当前电脑上做最小适配，保留12秒逻辑分段、0 overlap、原始RAW和人类确认门槛。先不要尝试付费 API 或自动录音。
5. macOS：检查「系统设置 → 隐私与安全性 → 麦克风」，确保运行 Python 的终端/应用获得权限。首次确认录音时系统可能弹授权提示。不要用无声录音自动绕过用户确认。

## 3. 启动并检查页面（仍不录音、不调用模型）

开两个终端，分别在包根目录运行：

| 系统 | 后端终端 | 前端终端 |
|---|---|---|
| macOS/Linux | `sh scripts/start-backend.sh` | `sh scripts/start-frontend.sh` |
| Windows PowerShell | `& .\scripts\start-backend.ps1` | `& .\scripts\start-frontend.ps1` |

后端固定监听本机 `127.0.0.1:8765`；前端为 `http://127.0.0.1:5173/`。先检查 `/api/health` 的 `ready=true`、`maximum_duration=6000`、`summary_enabled=true`、`microphone_status=selected` 和真实设备名；打开页面确认五门课程、导入按钮及实时课堂按钮。若麦克风未就绪，页面禁用「开始课堂」，而文件导入仍可用。不要为了腾端口关闭未知进程；查明占用后处理。后端启动本身不会录音或调用 SiliconFlow。

## 4. 人工验收：由用户亲自开始

必须执行 **PREPARE → STOP → USER ACTION → USER CONFIRMS → INSPECT**。完成上述配置/静态检查后，告诉用户测试文本和页面操作，然后停下；Codex 不替用户点击「开始课堂」，不自动打开麦克风、不自动发送付费请求。

可朗读约 30–45 秒：

> 现在测试这台电脑上的课堂转录。我们先讲状态空间模型，接着讨论 controllability，也就是系统的能控性。刚才我说 rank 等于二，啊不对，这个例子应该等于三。这里稍微停顿一下，然后继续说明输入如何影响系统状态。

请用户：选择「现代控制理论」→「开始课堂」→核对弹窗中的**实际麦克风名称**→确认开始→正常朗读→读完点击「停止」。不必录满100分钟；停止会提交不足12秒的尾段并等待转录/摘要收尾。用户确认完成后，检查：RAW片段逐步出现且有时间戳；右栏出现摘要；无漏段/重复段和明显错误；停止后状态回到 Idle；「保存记录」可用，记录出现在列表、刷新后仍在并可重命名。让用户听/看并评价是否可接受；质量判断不能只靠 mock 或服务器状态。

**音视频导入可选验收**：由用户自行选择一份短的本地 MP4/音频；先确认文件、顺序和总时长，再亲自点「开始导入」。仅检查原文件不变、RAW/摘要逐步出现、结果与实时记录并列。导入不使用麦克风；不要擅自处理用户私有文件。

## 5. 数据、限制与交付报告

- 用户 Key 在根目录 `.env`；本地记录在 `data/classroom_records/`；新录音在 `recordings/`；请求/摘要证据在 `outputs/`。这些目录和 `.venv`/`frontend/node_modules` 都被忽略，不属于迁移包。要迁移旧记录必须得到用户另外授权。
- 固定12秒边界仍可能断句/伤及跨界英文术语；RAW不经Cleaner改写。摘要基于已有RAW，用户应核对关键事实。最多100分钟是软件上限，并不等于目标电脑已经过100分钟实测。
- 停止导入会保留已经处理的内容，本版没有导入断点续传；超长文件或不支持音轨会被拒绝。真实远程API调用产生流量/费用。
- 如 Mac 上 PortAudio/Tk/FFmpeg 行为与 Windows 不同，在**本迁移包副本**内做必要兼容修复并记录；不要更改原电脑项目的冻结 V0 源码。
- 最后汇报：OS/CPU架构、Python/Node/FFmpeg版本、设备index/name/host API、doctor安全配置状态、health和前端结果、人工测试结果、问题及处理。绝不报告 Key 内容。人工测试未执行时，明确写「等待用户操作」，不可宣称真实链路PASS。
