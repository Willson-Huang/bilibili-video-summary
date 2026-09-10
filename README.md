<div align="center">

# bilibili-video-summary

[![Release](https://img.shields.io/github/v/release/Willson-Huang/bilibili-video-summary?display_name=tag&logo=github)](https://github.com/Willson-Huang/bilibili-video-summary/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Stars](https://img.shields.io/github/stars/Willson-Huang/bilibili-video-summary?style=social)](https://github.com/Willson-Huang/bilibili-video-summary/stargazers)

**把 B站视频，变成半年后还能搜到的知识笔记。**

字幕直取（秒级）或本地双引擎转写（whisper 快约 20 倍 / Fun-ASR-Nano 中文专名更准）→ 专名纠错 + 广告过滤 → 固定 14 节结构化条目，经结构校验后交付。

**EN** — Turn a Bilibili video into a knowledge note you can still search six months later: official subtitles in seconds, or local dual-engine ASR (whisper ≈20× faster, Fun-ASR-Nano far better on Chinese proper nouns), then proper-noun correction + ad filtering, and finally a fixed 14-section Markdown entry that must pass structure validation.

[Releases](https://github.com/Willson-Huang/bilibili-video-summary/releases/latest) · [更新日志](#-更新日志最新在上) · [真实输出示例](examples/2025-07-25_哲学知识分享——熵增与熵减_荣格不吃炸鸡_纪要.md) · [快速开始](#-快速开始三选一) · [FAQ](#-faq)

</div>

---

<!-- ══════════════════════════════════════════════════════════════════════
     维护规则（改动本区块前必读）
     1. 新版本一律插入本区块**最上方**，最新在前（不要追加到页面末尾）
     2. 历史条目**只增不删**：不删除、不改写、不合并旧版本条目
     3. 每个版本固定三段：版本号+日期（标题）→ 索引表加一行 → 条目明细
     4. 仅"最新版本"展开；更早版本收进 <details>，保证首屏清爽且历史可查
     ══════════════════════════════════════════════════════════════════════ -->

## 🆕 更新日志（最新在上）

| 版本 | 日期 | 主题 |
|---|---|---|
| **v2.5.2** | 2026-09-10 | 文档修正：AI 字幕经回译、专名不可信（含路由与验证基准的使用边界） |
| [v2.5.1](https://github.com/Willson-Huang/bilibili-video-summary/releases/tag/v2.5.1) | 2026-09-10 | 双引擎路由（whisper ↔ Fun-ASR-Nano）· 专名纠错表 · 白名单自学习 |
| [v2.2.0](https://github.com/Willson-Huang/bilibili-video-summary/releases/tag/v2.2.0) | 2026-09-05 | 结构校验 · Cookie 自动导出 · 队列按 UP主 过滤 · 合规加固 |
| [v2.1.0](https://github.com/Willson-Huang/bilibili-video-summary/releases/tag/v2.1.0) | 2026-08-30 | 首次发布：WorkBuddy 原版 + 跨平台便携版 |

### v2.5.2 — 2026-09-10　[Release Notes](https://github.com/Willson-Huang/bilibili-video-summary/releases/tag/v2.5.2)

- ⚠️ **修正一处会误导使用的表述**：此前文档把"有 B站官方字幕"写成「零识别错误，永远优先」——**这只对人工 CC 字幕成立**
- 🔍 **明确字幕来源分级**（素材包「字幕」行会标注，`--force-asr` 可强制跳过字幕走本地转写）：
  - `zh-CN` **人工 CC**（UP主 / 字幕组上传）→ 可信，可直接用，**可作本地转写的核对基准**
  - `ai-zh` **B站 AI 生成** → **经中文 → 英文 → 中文回译**，地名 / 人名 / 机构名偏差可能很大（同音替代 + 回译错译），**不可作验证基准**
- 🧭 **路由与验证的使用边界**：专名密集内容（历史 / 地理 / 政经）即使有 AI 字幕，也建议 `--force-asr --engine funasr-nano` 走本地转写；用"字幕对照"验证引擎时，基准必须用人工 CC 字幕，只有 AI 字幕时结论需再用常识复核
- 📝 同步修正：README 流程图说明、FAQ、两版 SKILL.md 的路由逻辑与引擎分流表

### v2.5.1 — 2026-09-10　[Release Notes](https://github.com/Willson-Huang/bilibili-video-summary/releases/tag/v2.5.1)

- 🧠 **双引擎路由**：新增 **Fun-ASR-Nano** 支持，专治中文专名同音误识——实测专名正确率 **10/11 vs whisper 2/11**（"隐性债务"whisper 错成"险性债务"×5，Nano 全对）。`--classify` 按 UP主白名单 → 视频 tag → 关键词打分给出引擎建议
- 📕 **专名纠错表**：新增 `references/asr_glossary.txt`（机器可读的 `误识 | 正确 | 语境限定`）+ `check_glossary.py` 兜底复核，拦截"看起来没毛病的合法中文词"类误识
- 🔁 **自学习白名单**：`references/engine_up_whitelist.txt` + `engine_verify_log.txt`——UP主 → 引擎的映射可增量维护，判错留痕可追溯（本仓库只提供**模板**与维护规则，不含个人观看清单）
- 🔧 **队列透传**：`process_queue.py --engine funasr-nano --hotwords <文件>`，批量队列不再悄悄跑回 whisper
- 🐛 修复：`requested_downloads` 为空列表时索引崩溃；批量队列不再清空用户手写备注
- 🧩 重构：WBI 签名逻辑统一到 `bili_wbi.py`；路径全部改为 `__file__` / 环境变量派生（可迁移、可便携部署）
- ⚖️ **合规**：撤下第三方"B站非公开接口文档"映射表（该上游因**律师函**已关停并明令禁止再分发），改为只依赖公开 tag 接口

<details>
<summary><b>v2.2.0 — 2026-09-05</b>　结构校验 · Cookie 自动导出 · 队列按 UP主 过滤（点击展开）</summary>

- ✅ **结构校验**：新增 `verify_structure.py`——frontmatter 齐全、tags/entities 数量、14 节齐全、要点表格化，四项硬拦截（源于产物退回旧模板的生产事故）
- 🍪 **Cookie 自动导出**：新增 `chrome_cookie_export.py`——解密 Chrome 存储的 B站 Cookie 写入凭据文件（支持 v10/v11 加密；Chrome 127+ 的 v20 会识别并提示手动配置）
- 🎯 **按 UP主 过滤**：`library_queue.py --up <UP主>`，在线表队列按系列分批处理
- 🐛 修复：字幕直取路由下 `asr` 为 `null` 时台账回填崩溃
- 🗄️ **素材包归档制**：转写全文不再随收尾删除（归档至 `cache/bili_subs/`，注意存在 `sub_BV*` / `bili_BV*` 两种前缀），ASR 误识修正随时可回查
- 📖 新增完整文档：断点恢复三步交叉核实法、Cookie 持久化配置、IMA 归档踩坑清单（20+ 条）
- 📝 README 全面重做：真实性能数据、Mermaid 流水线图、三路快速开始、真实输出示例、FAQ
- ⚖️ 合规加固：输出示例与模板新增版权声明、24h 删除通道、付费内容使用限制

</details>

<details>
<summary><b>v2.1.0 — 2026-08-30</b>　首次发布（点击展开）</summary>

- 🚀 **双版本首发**：`workbuddy/`（drop-in 安装，含资料库在线表队列 + IMA 归档）+ `portable/`（无平台依赖，可装到 Claude / Cursor / 自定义 GPT）
- 🎧 本地 Whisper 离线转写：faster-whisper + yt-dlp，GPU 加速；音轨不转码，转写完即删
- 🧱 固定 14 节知识库条目模板 + 广告口播两级过滤（70 词表预标记 + AI 复核）
- 🧩 公共 WBI 签名模块 `bili_wbi.py` / `bili_wbi.mjs`（消除三处重复实现）
- 🧪 `tests/test_core.py` 纯本地自测（13 项断言，覆盖链接解析 / 段落合并 / 广告撞车词 / 时长解析）
- ➕ `requirements.txt` 依赖声明、`.gitattributes` 行尾规范（`*.sh` 强制 LF）
- 🔒 发布前隐私脱敏：本机路径参数化、私有资源 ID 占位化、真实视频数据中性化

</details>

---

## 为什么做这个

听 1 小时播客要花 1 小时；让 AI「总结这个视频」，它只会看标题和简介——**它根本没看过视频**。

这个工具把音轨拉下来，用本地 Whisper 转写成**带时间戳的全文**，再基于全文产出 14 节知识条目：每条结论可回跳时间点，每个存疑处标注 `[原文疑似]`，转写数据不出你的电脑。

| ⏱️ 实测性能（RTX 4060 Ti） | |
|---|---|
| 2 分 24 秒视频 → 拿到全文转写 | **12 秒**（下载 2.0s + 转写 8.8s） |
| 1 小时视频推算 | **约 3 分钟**（large-v3-turbo @ CUDA） |
| 4 分 48 秒完成 1:39:28 长视频实测 | **24.6x 实时** |
| 播放视频？上传数据？ | 不需要 · 不上传 |

> 上表为真实运行记录（`BV1habQzWEzq`，turbo @ CUDA，模型已缓存）。**首次运行**会额外下载 Whisper 模型（约 1.5 GB，一次性的），之后走本地缓存。

## 它是怎么工作的

```mermaid
graph LR
    A["B站链接 BV/av/b23.tv"] --> B{"能否字幕直取"}
    B -->|有CC或AI字幕| C["拉取字幕全文 秒级"]
    B -->|无字幕或未登录| D["下载音轨m4a"]
    D --> E{"引擎路由 classify"}
    E -->|中文专名密集| F["Fun-ASR-Nano 转写"]
    E -->|泛科技或英文术语| G["whisper 转写 约20x"]
    C --> H["带时间戳素材包"]
    F --> H
    G --> H
    H --> I["专名纠错表 + 广告过滤"]
    I --> J["14节知识库条目"]
    J --> K["结构校验 verify_structure"]
```

**三条路径怎么走**：

| 环节 | 说明 |
|---|---|
| **字幕直取** | 配置 B站 Cookie 后直接拉取字幕（秒级）；无字幕或未配置 Cookie 时自动降级为本地转写。音轨下载后不转码、转写完即删。⚠️ **"有字幕"不等于可信**：`zh-CN` = 人工 CC（可信，可作核对基准）；`ai-zh` = B站 AI 生成，**经中文 → 英文 → 中文回译**，地名人名偏差大，**不可作验证基准**——专名密集内容建议 `--force-asr` 走本地 `funasr-nano` |
| **引擎路由** | 无字幕时按 `UP主白名单 → 视频 tag → 关键词打分` 选引擎（`--only-meta --classify` 给出建议）：`whisper` 快约 20 倍，`Fun-ASR-Nano` 中文专名更准（实测 10/11 vs 2/11）；灰区一律判 whisper |
| **纠错与校验** | `asr_glossary.txt` 强制纠正"看起来没毛病的合法中文词"类误识；产出必须过 `verify_structure.py` 四项硬拦截（frontmatter / tags·entities 数量 / 14 节齐全 / 要点表格化）|

## 🚀 快速开始（三选一）

<details open>
<summary><b>🤖 我是 Claude / Cursor / ChatGPT 用户（最常见）</b></summary>

1. 下载并解压 [便携版 zip](https://github.com/Willson-Huang/bilibili-video-summary/releases/latest)（或 `git clone` 本仓库）
2. 安装依赖：`pip install -r portable/requirements.txt`
3. 把 `portable/SKILL.md` **全文**作为指令导入：Claude Projects / Cursor Rules / GPTs Instructions
4. 之后对 AI 说：*「总结这个视频 https://www.bilibili.com/video/BVxxxx」* 即可

</details>

<details>
<summary><b>🛠️ 我是 WorkBuddy 用户（一键安装）</b></summary>

1. 下载 [WorkBuddy 版 zip](https://github.com/Willson-Huang/bilibili-video-summary/releases/latest)
2. 解压，把 `bilibili-video-summary/` **整个文件夹**放进 `~/.workbuddy/skills/`
3. 重启 WorkBuddy，直接发 B站链接
4. 首次使用前装转写环境：`pip install faster-whisper yt-dlp imageio-ffmpeg`（详见 Release Notes）

</details>

<details>
<summary><b>👨‍💻 我只想用脚本，不接 AI 平台</b></summary>

```bash
git clone https://github.com/Willson-Huang/bilibili-video-summary.git
cd bilibili-video-summary/portable
pip install -r requirements.txt

# 转写一个视频（首次运行会自动下载 Whisper 模型）
python scripts/bili_asr.py "https://www.bilibili.com/video/BVxxxx" --out 素材包/demo.md

# 自测环境（不联网、不需要模型）
python tests/test_core.py
```

</details>

## 📄 输出长什么样

一段 2 分 24 秒的科普短视频（《熵增与熵减》），完整真实产出见
[examples/2025-07-25_哲学知识分享——熵增与熵减_荣格不吃炸鸡_纪要.md](examples/2025-07-25_哲学知识分享——熵增与熵减_荣格不吃炸鸡_纪要.md)：

> **版权说明**：示例纪要基于上述 UP主的公开视频由本工具自动整理，仅供演示输出格式与个人学习使用；视频内容及音轨版权归原 UP主所有。如权利人要求删除或调整，请提 [Issue](https://github.com/Willson-Huang/bilibili-video-summary/issues) 或联系仓库所有者，将在 24 小时内处理。

<details>
<summary><b>点开看真实产出片段</b></summary>

**核心结论**：熵增是孤立系统自发走向混乱的物理铁律，而生命通过持续汲取能量维持自身秩序，是对抗熵增的"熵减堡垒"。

| 时间 | 要点 |
|---|---|
| 00:00:26 | 热力学第二定律：孤立系统自发从有序走向无序（冰块融化 / 墨水扩散 / 快递拆封） |
| 00:00:48 | 熵减必须消耗外部能量：整理房间对抗混乱 |
| 00:01:07 | 生命体靠汲取能量维持有序，是"对抗熵增的堡垒" |

**术语表**（编者补充以增强检索）：熵 / 熵增定律 / 耗散结构 / 负熵……

**诚实标注 ASR 误识**——转写把「熵增」识别成「伤增」、「无序」识别成「无需」。
所有存疑处都标 `[原文疑似]` 并记录在条目的「信息完整性」章节，而不是假装正确：

| 转写原文 | 应为 |
|---|---|
| 伤增 | 熵增 |
| 无需洪流 | 无序洪流 |
| 商简之火 | 熵减之火 |

</details>

## 📦 下载安装 / Download & Install

不想用 git？直接从 [**Releases**](https://github.com/Willson-Huang/bilibili-video-summary/releases/latest) 下载打包好的安装包：

| 资产 | 用途 |
|---|---|
| `bilibili-video-summary-workbuddy-vX.Y.Z.zip` | 解压后把 `bilibili-video-summary/` 整个放进 `~/.workbuddy/skills/`，重启 WorkBuddy 即完成安装 |
| `bilibili-video-summary-portable-vX.Y.Z.zip` | 解压到任意位置，把 `SKILL.md` 作为指令导入你的 AI 平台 |
| `checksums.txt` | 各资产 SHA-256，下载后建议核对 |

## ❓ FAQ

<details>
<summary>需要 B站账号 / Cookie 吗？</summary>

不需要。不配 Cookie 也能下载音轨并转写；配置 Cookie（`SESSDATA`）后可解锁 CC/AI 字幕直取（秒级，不用转写）。

</details>

<details>
<summary>视频有 B站 AI 字幕，是不是就不用本地转写了？</summary>

**看字幕来源，别看"有没有字幕"。**

| 来源标识 | 是什么 | 能不能直接用 |
|---|---|---|
| `zh-CN` | 人工 CC（UP主 / 字幕组上传） | ✅ 可信，直接用，还能当本地转写的核对基准 |
| `ai-zh` | B站 AI 生成 | ⚠️ **慎用** |

`ai-zh` 是机器生成，且**经中文 → 英文 → 中文回译**：同一段口播先被识别/转成英文、再译回中文，地名、人名、机构名会成片偏移（同音替代叠加回译错译）。这类字幕拿来做"大概了解内容"没问题，**但不能作为专名的依据，也不能作为评估其他引擎的基准**。

**做法**：专名密集的内容（历史 / 地理 / 政经），即使有 AI 字幕，也建议 `--force-asr --engine funasr-nano` 强制走本地转写；泛科技、口语向内容用 AI 字幕省时间是合理的。素材包的「字幕」行会标注来源，AI 字幕会带「（AI 生成，可能有错字）」提示。

</details>

<details>
<summary>没有 NVIDIA GPU 能用吗？</summary>

能。脚本自动回退 CPU（int8），速度约为 GPU 的 1/6——1 小时视频约 9–10 分钟。有 N 卡就装 `nvidia-*` 包，自动启用 CUDA。

</details>

<details>
<summary>和「AI 直接总结视频链接」有什么区别？</summary>

AI 平台看不到视频内容，只能基于标题/简介/评论猜。本工具把<b>带时间戳的全文转写</b>喂给 AI：总结基于真实内容，每条结论都能回跳到视频时间点，还能按你的知识库格式归档。

</details>

<details>
<summary>支持哪些链接格式？</summary>

`bilibili.com/video/BVxx`、`b23.tv` 短链、`av` 号、`?p=N` 分P。多个链接可走本地 CSV 队列批量处理（`process_queue.py`）。

</details>

## 🔒 隐私：数据不出本机

- 音轨下载、Whisper 转写、纪要生成**全程在本地完成**，无任何云端调用；Whisper 模型从 HuggingFace 镜像一次性下载后离线使用
- B站登录 Cookie 只存本地文件（`~/.cache/bilibili-video-summary/.bilibili_cookie`），已被 `.gitignore` 排除，**永远不会进仓库**
- 本仓库发布前做过脱敏审计：无绝对路径、无私有资源 ID、无真实用户数据；脚本与文档中的敏感值全部改为环境变量（`BILI_PYTHON` / `BILI_CACHE` / `BILI_COOKIE` 等）

## 两个版本

| 目录 | 适用场景 | 依赖 |
|---|---|---|
| [`workbuddy/`](./workbuddy) | 在 **WorkBuddy** 里使用，保留「资料库在线表」队列 + IMA 知识库归档 | WorkBuddy 运行时、IMA MCP |
| [`portable/`](./portable) | 装到 **Claude / Cursor / ChatGPT 自定义 GPT** 等任意 AI 平台 | 仅标准 Python/Node + `faster-whisper`/`yt-dlp`/`imageio-ffmpeg` |

## 目录结构

```
bilibili-video-summary/
├── examples/            # 真实产出示例（本工具自己生成的 14 节纪要）
├── workbuddy/           # WorkBuddy 原版（SKILL.md + scripts + references + tests）
└── portable/            # 便携版（无平台依赖）
    ├── SKILL.md         # 完整指令——导入 AI 平台的就是它
    ├── requirements.txt
    ├── scripts/         # bili_asr.py / process_queue.py / bili.mjs / bili_wbi.py|mjs ...
    │                    #   funasr_adapter.py 第二引擎（Fun-ASR-Nano）适配层
    │                    #   check_glossary.py 专名纠错复核 · verify_structure.py 结构校验
    │                    #   verify_coverage.py 覆盖校验 · chrome_cookie_export.py Cookie 导出
    ├── references/      # 知识条目模板 / 广告过滤词表
    │                    #   asr_glossary.txt 专名误识对照表
    │                    #   engine_up_whitelist.txt + engine_verify_log.txt（模板）
    └── tests/           # 纯本地自测（13 项断言）
```

## 注意事项

- 视频内容的版权归原作者所有；本工具仅作个人学习与知识整理用途，请勿批量抓取或分发他人内容
- **不得用于下载、绕过或分发付费 / 大会员专属内容**；配置 Cookie 仅用于访问你本人账号有权查看的内容（字幕直取 / 高码率音源）
- 公开发布由本工具生成的条目时，请附视频链接与版权归属说明（模板「使用规则」已内置此要求）
- 模型缓存：`~/.cache/bilibili-video-summary/models/whisper`（便携版）或 `~/.workbuddy/models/whisper`（WorkBuddy 版）
- 转写是口播内容，含口语重复与 ASR 错字；产出中疑问处一律标注 `[原文疑似]`

---

<div align="center">

如果这个工具帮你省下了刷视频的时间，欢迎点个 ⭐ —— 对一个刚出生的仓库很重要。

**MIT License** · 由 [@Willson-Huang](https://github.com/Willson-Huang) 为自己的 Obsidian 知识库而造，发布前已完成隐私脱敏与本地自测

</div>
