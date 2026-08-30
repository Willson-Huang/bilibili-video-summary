# bilibili-video-summary

**English** — Turn a Bilibili (B站) video link into a knowledge note you can still search six months later: local Whisper transcription (offline, GPU-accelerated) plus a fixed 14-section Markdown template (YAML metadata, retrieval-index table, entity table, timeline, claims-to-verify list, glossary). No playback needed, no waiting for the video to run in real time.

**中文** — 把 B站（bilibili）视频链接 / BV号 / av号 / b23.tv 短链，转成本地 Whisper 离线转写 + 14 节结构化知识库条目（YAML 元数据、检索入口表、实体表、时间线、待验证清单、术语表）。本地 GPU 批处理，**不需播放、不需等待实时时长**。

> Detailed documentation below is in Chinese. 完整文档见下方中文部分（环境变量、批量队列、广告过滤、隐私说明等）。

## Quick Start / 快速开始

```bash
cd portable
pip install -r requirements.txt       # faster-whisper / yt-dlp / imageio-ffmpeg
python scripts/bili_asr.py "https://www.bilibili.com/video/BVxxxx" --out 素材包/bili_BVxxxx.md
# 读回素材包，按 references/knowledge-entry-template.md 生成 14 节纪要
# Then read the transcript back and write the 14-section note using the template above

python tests/test_core.py             # 自测（不联网、无需额外依赖）/ offline self-test
```

**Requires / 环境要求**：Python 3.10+；Node ≥ 18（可选，仅 `bili.mjs` / `set-cookie.mjs` 需要）。

## 📦 下载安装 / Download & Install

不想用 git？直接从 [**Releases**](https://github.com/Willson-Huang/bilibili-video-summary/releases/latest) 下载打包好的安装包：

| 资产 | 用途 |
|---|---|
| `bilibili-video-summary-workbuddy-vX.Y.Z.zip` | 解压后把 `bilibili-video-summary/` 整个放进 `~/.workbuddy/skills/`，重启 WorkBuddy 即完成安装 |
| `bilibili-video-summary-portable-vX.Y.Z.zip` | 解压到任意位置，把 `SKILL.md` 作为指令导入 Claude Projects / Cursor Rules / Custom GPT |
| `checksums.txt` | 各资产 SHA-256，下载后建议核对 |

> WorkBuddy 版首次使用前需准备转写环境（`pip install faster-whisper yt-dlp imageio-ffmpeg`），详见 Release Notes。

---

## 两个版本 / Two editions

能力等价，依赖不同：

| 目录 | 适用场景 | 依赖 |
|---|---|---|
| [`workbuddy/`](./workbuddy) | 在 **WorkBuddy** 里使用，保留「资料库在线表」队列 + **IMA** 知识库归档 | WorkBuddy 运行时、IMA MCP、平台在线表 |
| [`portable/`](./portable) | 装到 **Claude / Cursor / ChatGPT 自定义 GPT** 等任意支持自定义指令的 AI 平台 | 仅标准 Python/Node + `faster-whisper`/`yt-dlp`/`imageio-ffmpeg` |

延伸阅读：

- 完整指令见 [`portable/SKILL.md`](./portable/SKILL.md)
- 批量处理见 [`portable/scripts/process_queue.py`](./portable/scripts/process_queue.py)（本地 CSV 队列）
- 广告过滤词表、知识条目模板见 [`portable/references/`](./portable/references)

## 目录结构

```
bilibili-video-summary/
├── README.md
├── LICENSE              # MIT
├── .gitignore
├── .gitattributes       # 固定 *.sh 为 LF、*.bat 为 CRLF，避免跨平台脚本失效
├── workbuddy/           # 原版（WorkBuddy + IMA）
│   ├── SKILL.md
│   ├── scripts/         # 含 library_queue.py / ima_cos_upload.py（平台强依赖组件）
│   ├── references/
│   └── tests/
└── portable/            # 便携版（通用 Markdown，无平台依赖）
    ├── SKILL.md
    ├── requirements.txt
    ├── scripts/         # bili_asr.py / process_queue.py / bili.mjs / bili.bat / bili.sh ...
    │                    # bili_wbi.py|mjs 为 WBI 签名公共模块，被其余脚本共用
    ├── references/      # knowledge-entry-template.md / ad_keywords.txt
    └── tests/           # test_core.py：纯本地自测
```

## 隐私与安全说明

本仓库发布前做过脱敏，已处理：

| 类型 | 处理方式 |
|---|---|
| 本机绝对路径（`C:/Users/<用户名>/...`、Obsidian 库路径） | 参数化为环境变量（`BILI_PYTHON` / `BILI_RAW_DIR` / `BILI_CACHE_DIR` / `BILI_CSV` 等），默认值用 `~` 相对路径 |
| 私有资源 ID（资料库 `database_id`、IMA `kb_id`、空间直链） | 改为 `<YOUR_DATABASE_ID>` / `<YOUR_IMA_KB_ID>` 占位符，用环境变量 `BILI_DATABASE_ID` 注入 |
| 真实处理过的视频数据（BV号 / UP主 / 标题 / 时间） | `--init` 示例行改为中性占位（`BV0000000000` / 示例UP主） |
| 硬件信息 | GPU 型号泛化为「NVIDIA GPU」 |
| Cookie / 凭据 | 从未入库；`.bilibili_cookie`、`.env` 等已被 `.gitignore` 排除 |

使用 `workbuddy/` 版前需回填你自己的资源 ID（环境变量或直接改占位符）；`portable/` 版无需任何回填即可运行。

## 注意事项

- **不要提交凭据**：Cookie 文件（`.bilibili_cookie`）、`.env`、台账 CSV、转录产物均被 `.gitignore` 排除。
- 模型缓存默认 `~/.cache/bilibili-video-summary/models/whisper`（便携版）或 `~/.workbuddy/models/whisper`（原版）。
- 视频内容的版权归原作者所有；本工具仅作个人学习与知识整理用途，请勿批量抓取或分发他人内容。
