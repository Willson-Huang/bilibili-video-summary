---
name: bilibili-video-summary
description: 用户发送 B站（bilibili）视频链接、BV号、av号或 b23.tv 短链，要求总结视频观点/要点/内容，或要求把视频内容整理成知识库条目时使用。通过本地 Whisper 模型下载音频并离线转写（GPU 加速），再基于转写全文生成 14 节知识库条目（含 YAML 元数据、检索入口表、实体表、时间线、待验证清单、术语表）。触发词：B站、bilibili、BV号、b23.tv、这个视频讲了什么、总结视频、视频要点、存知识库、知识库条目、归档。
version: 2.5.2
agent_created: true
---

# B站视频观点总结

## 核心能力
发链接 → 自动转写 → 生成知识库条目。本地 GPU 离线批处理，**不需播放、不需等待实时时长**。

产出**只有一种格式**：14 节知识库条目（含 YAML 元数据、检索入口表、实体表、时间线、待验证清单、术语表）。用户存这些文件是为了半年后还能搜到，所以结构固定，不做精简版。

## 一键入口
```bash
cd <工作区>
"~/.workbuddy/skills/bilibili-video-summary/scripts/bili.bat" "<B站链接>"
```
`bili.bat` 已封装所有必需环境变量，直接用。

## 运行环境
| 项 | 值 |
|---|---|
| Python | `~/.workbuddy/binaries/python/envs/whisper/Scripts/python.exe` |
| 主脚本 | `scripts/bili_asr.py` |
| 模型缓存 | `~/.workbuddy/models/whisper`（medium / large-v3 / large-v3-turbo 已就绪） |
| 硬件 | NVIDIA GPU → CUDA 加速 |
| 配套脚本 | `bili.mjs`（Node 轻量替代）、`search_bili.py`（按关键词搜视频）、`verify_coverage.py`（覆盖校验） |
| 参考模板 | `references/knowledge-entry-template.md` |

基目录：`~/.workbuddy/skills/bilibili-video-summary/`

## 必须的环境变量（已写入 bili.bat）
| 变量 | 值 | 不设会怎样 |
|---|---|---|
| `PYTHONPATH` | 空 | WorkBuddy 注入的 shim 拦截文件删除 → HF 下载 / pip 装大包失败 |
| `HF_HUB_DISABLE_SYMLINKS` | `1` | Windows 符号链接解析失败 → `model.bin is incomplete` |
| `HF_HUB_DISABLE_XET` | `1` | hf-mirror 的 xet 后端返回 401 |

## 工作流

### 1. 提取链接
支持 `bilibili.com/video/BVxxx`、`b23.tv/xxx`、`av123`、`?p=N` 分P。多个链接 → 逐个执行。

### 2. 转写
```bash
# 单条：先落缓存，别直接写进 raw
bili.bat "<链接>" --model large-v3-turbo --out ".workbuddy/cache/bili/bili_<BV>.md"
```
| 参数 | 说明 |
|---|---|
| `--model` | `large-v3-turbo`（默认，速度与精度平衡）/ `large-v3`（最准）/ `medium`（最快） |
| `--p N` | 指定分P |
| `--force-asr` | 跳过 B站字幕直取，强制本地转写 |
| `--cookie "..."` | 会员视频 / 高码率音源 / 触发字幕路由时使用 |
| `--keep-audio` | 保留音频（默认转写后删除） |
| `--only-meta` | 只抓元信息（view 接口），判断视频是否值得细看；不请求字幕/章节/评论接口，抓到的 view 落缓存供后续转写复用 |
| `--batch-file <json>` | **批处理模式**：`[{"url":..., "out":...}]`，整批只加载一次模型 |
| `--engine whisper\|funasr-nano` | 本地 ASR 引擎，默认 `whisper`；`funasr-nano` 中文专名更强（见下方「引擎路由」） |
| `--hotwords <文件>` | 热词文件路径，仅 `--engine funasr-nano` 生效；每行一词，去重后硬上限 80 |

**单条**与**批处理**共用同一套逻辑，区别只在模型加载次数。批量任务一律走批处理。

### 3. 路由逻辑（自动）
1. **字幕直取**（秒级）：需登录态 + 视频开放 CC/AI 字幕 → 直接用
2. **本地 ASR**（分钟级）：无字幕或匿名模式 → 下载音轨 + GPU 转写
3. 返回 JSON 的 `route` 字段标明实际走了哪条路

> ⚠️ **"有字幕"不等于"可信"——先看字幕来源**（素材包「字幕」行会标注，`--force-asr` 可强制跳过字幕走本地转写）
>
> | 字幕来源 | 判定 | 用法 |
> |---|---|---|
> | `zh-CN`（**人工 CC**，UP主 / 字幕组上传） | 人工产出 | 可信，直接用；**可作为本地转写的核对基准** |
> | `ai-zh`（**B站 AI 生成**） | 机器生成，**经中文 → 英文 → 中文回译** | 地名、人名、机构名偏差可能很大（同音替代 + 回译错译）。**不可作为验证基准**；专名密集内容即使有 AI 字幕，也建议 `--force-asr --engine funasr-nano` 走本地转写，或至少与本地转写对照后再落纪要 |
>
> 脚本会自动给 AI 字幕加「（AI 生成，可能有错字）」标记——**看到这行就别把它当标准答案**。
> 同理：用"字幕对照"验证引擎时，基准必须用**人工 CC 字幕**；只有 AI 字幕时，结论需再用常识复核一遍（AI 字幕本身可能已经错了）。

### 3b. 引擎路由：whisper 与 Fun-ASR-Nano 怎么选（2026-09-06 实测定稿）

两段独立 60s 样本实测（基准优先取人工 CC 字幕，见上表）：

| 模型 | 专名正确率 | 典型表现 |
|---|---|---|
| whisper-large-v3-turbo | 2/11（18%） | 肇庆→赵庆、怀集→淮吉、利玛窦→立马逗、赵佶→赵吉 |
| **Fun-ASR-Nano-2512（800M）** | **10/11（91%）** | 肇庆/怀集/赵佶/端王/利玛窦全对，仅"立桥楼→历桥楼"近似错 |
| Qwen3-ASR-1.7B | 7/11（64%） | **肇庆→赵庆（同 whisper 错）**、利玛窦→利马窦 |

**分流规则**（默认仍 whisper，Nano 需显式 opt-in）：

| 内容类型 | 引擎 | 理由 |
|---|---|---|
| 有**人工 CC 字幕**（`zh-CN`） | 字幕直取（不用 ASR） | 人工产出，可信度最高，优先 |
| 只有 **AI 字幕**、内容专名密集（历史/地理/政经） | `--force-asr --engine funasr-nano` | AI 字幕是回译产物，专名偏差大，宁可本地转写 |
| 只有 **AI 字幕**、内容泛科技/口语向 | 字幕直取（可接受） | 非专名场景回译影响小，秒级省事 |
| 历史/地理/纪实/政经/人名地名密集（历史/地理/纪实类系列） | `--engine funasr-nano` | 专名识别显著更强，实测 91% vs 18% |
| 泛聊天/通用科技/快速批量初筛 | `whisper`（默认） | 速度快、常驻成本低 |
| 需要逐字引用 + 专名密集 | `funasr-nano` + `--hotwords` | 热词进一步兜底近音政区名 |

**触发 Nano 的条件**（满足任一即考虑，不要只看置信度——whisper 的"肇庆→赵庆"很可能是高置信度错）：
- 频道/视频类别属高专名风险（历史、地理、纪实、政经）
- 文本出现疑似音译地名/人名
- 两个引擎结果不一致时，以 Nano 为准

> **我该什么时候切**：见下一节 3c「AI 侧自动切换规则」——脚本不会自动切，由接收任务时按 UP主白名单/标题关键词判断并显式带 `--engine`。

**速度（RTX 4060 Ti 8GB，实测）**：

| 场景 | 结果 |
|---|---|
| whisper turbo 60s 音频 | 载 4s / 转 2.9s（约 20x 实时） |
| Nano 60s 音频（单 VAD 段） | 载 50-60s / 转 10s（约 6x 实时） |
| Nano 198s 音频（跨多 VAD 段） | 转 98.6s（约 **2x** 实时） |
| Nano 35min 长视频端到端 | 约 11.5 分钟（含下载+加载） |

**分段开销是真实的**：Nano 按 VAD 切段处理（默认 60s/段），跨段后实时倍率从 6x 降到 2x——热词也随每段重新注入。这与"热词零成本"的直觉相反，长视频必须按这个倍率估算时间，不要按单段 6x 外推。

**语言参数**：Nano 侧默认 `auto`（自动检测）。**不要强制 `中文`**——实测英文歌被强制中文识别后会输出"汝是汝，那会儿敢来迷住阮"这类错字。主脚本 `--lang zh`（默认）会映射为 auto。

**批量场景**：`--batch-file` + Nano 时，脚本先下载全部音频，再**一次加载模型处理整批**（`funasr_adapter.py --batch-json`），避免每条重复 50-70s 冷启动。单条 Nano 调用仍是"每条一次冷启动"，只适合单发。

**批量语义（重要）**：
- 结果**严格按输入 items 顺序**返回；中间项失败也留在原位，不会串位（上游队列按序 `zip` 回填台账，乱序会把 A 的状态写到 B 上）
- `model_load_sec` 只记在首条（批级开销），`asr_sec` 是**逐条**实际 generate 用时——台账「转写耗时(秒)」不会把整批总耗时重复记到每条
- 批量结束按 `--keep-audio` 清理原始音频；adapter 内部转码的 16k WAV 用完即删；Cookie 用一次性临时文件并在 `finally` 删除

**架构约束（重要）**：Nano 跑在**独立 venv** `~/.workbuddy/binaries/python/envs/asr_eval`（funasr 依赖与 faster-whisper/CTranslate2 冲突，绝不合并装）。主脚本经 `funasr_adapter.py` 子进程调用，输出归一为 `[{start, end, text}]` 写入同一套素材包格式——**接入与回退都不影响现有 14 节纪要流程**。

**Nano 输出归一**：Fun-ASR-Nano 不返回句子级结构（`sentence_info` 为 None），adapter 从 token 级 `timestamps`（单位秒，对应 LLM 正确分支；不要用 `ctc_text`，那是较弱的 CTC 分支，错误模式与 whisper 同款）按标点/间隔聚合成段落，并清理孤立标点段，保证素材包正文是 `[hh:mm:ss]` 可读段落。

**已知限制（别踩）**：
- **英文/音乐内容走 whisper**：Nano 的英文 token 拼接偶发粘连（如 `strangerstolove`），中文场景无此问题；英文歌等外语内容用 whisper 更稳
- **事件标签已过滤**：`[noise]`、`[SP K]`、`[BGM]` 等纯标签段不会进素材包正文
- **依赖装法**：funasr 的 `numpy<2` 约束在 Python 3.13 下已过时，必须 `pip install --no-deps funasr` + 手动装 numpy 2.x；**不要**试图把 funasr 装进 whisper venv

**热词（hotwords）使用规范**：
- **不是零成本开关**：VAD 每个分段都会重新注入热词前缀，长视频切 35-60 段 = 热词被处理几十次（不是一次性）
- 默认 **20-50 词**，**硬上限 80**（超长会注意力稀释、误插词）
- 来源：标题、简介、UP主名、已知人物/地名/机构、频道专属词表；**去重 + 按相关性排序**
- 文件不存在不阻断转写，但元数据会标 `hotwords_status: missing`，**stderr 会警告**（提交了热词文件却看到该状态时，先修正路径再采信专名结果）。重复项与 `#` 注释行自动过滤
- 热词状态四态：`loaded` / `empty`（文件在但无有效词）/ `missing`（路径不存在）/ `disabled`（未传）
- **空转写保护**：Nano 返回 `timestamp_status`，`ok` 才入库；`no_ts`（有正文但解析不出时间戳）与 `empty`（静音/无内容）一律判**失败**，不生成空素材包——知识库依赖 `[hh:mm:ss]` 引用，无时间戳的素材包不许进流程
- 元数据记录有效词表的 `hotwords_sha256`、源文件的 `hotwords_source_sha256`、数量、截断与状态，供追溯"某个专名为何被这样识别"

### 3d. 新 UP 主怎么办（--classify 自动判定 + 白名单自学习）

脚本内置 `--classify`，**不用下载音频、不跑 ASR**，只读 view 元信息（走 24h 缓存，0.3s）给出引擎建议：

```bash
bili_asr.py <链接或BV> --only-meta --classify
# 返回 engine_suggestion: {engine, confidence, source, reason, hits}
```

**判定优先级**（数据源实测结论，2026-09-06）：

| 优先级 | 信号 | 置信度 | 可靠性 |
|---|---|---|---|
| 1 | **UP主白名单** `references/engine_up_whitelist.txt` | `high` | 最准（已验证的） |
| 2 | **视频 tag**（官方 `x/tag/archive/tags` 接口） | `high` | **比分区准**：实测地理科普视频的 tag 全是地名 |
| 3 | **分区 tid_v2**（可选，需自行本地补充映射表） | `mid` | 仅供辅助：实测地理类视频被归到「知识·商业财经」，不准 |
| 4 | 标题/简介关键词 + 模式打分 | `mid`/`low` | 无词典维护成本，兜底 |
| 5 | 默认 | `default` | whisper |

**数据源与合规（必读）**：

| 信号 | 来源 | 合规状态 |
|---|---|---|
| 视频 tag | 官方公开接口 `x/tag/archive/tags` | ✅ 公开接口，主信号 |
| 标题 / 简介 / UP主 | view 接口公开字段 | ✅ 公开 |
| 分区 tid_v2 映射 | 第三方整理的接口文档 | ⚠️ **不随本仓库分发** |

> **分区映射表为何不随仓库发布**：该映射源自第三方"B站非公开接口文档"整理项目，**该项目已被 B站委托律所发函后关停并删除全部文档**（指控其系统性整理并公开非公开 API 的调用逻辑与参数结构）。
>
> **本仓库的处理**：不包含任何此类映射表；如需分区信号，请自行评估合规性后本地补充（脚本在映射表缺失时自动降级，只跳过该信号）。
> **更安全的主信号是视频 tag**——公开接口、实测比分区准，脚本已将其列为第 2 优先级。

> **分区为什么只能当辅助**：B站分区由 UP主投稿时自选，地理科普类会被归到「知识·商业财经」或「生活·日常」；而**视频 tag 由 UP主+观众共同产生，直接反映内容主题**，实测准确率明显更高。所以顺序是 tag 优先于分区。

**自学习机制**（白名单增量维护）：
1. 新 UP 主首次处理 → `--only-meta --classify` 拿关键词建议
2. 按建议跑完后，对照官方字幕/常识核对素材包里的专名
3. 结论确认后**追加一行到 `references/engine_up_whitelist.txt`**（格式 `UP主名 | 引擎 | 依据 | 日期`）
4. 下次同 UP 主直接 `high` 命中白名单，不再猜；**判错的条目不要删，改判并留一行修正说明**（保留可追溯）

> 这是"规则 + 数据分离"的自学习：规则写在 SKILL（我读），数据写在 references（可增量、可审阅）。不做黑盒自动学习——误判污染白名单的代价高于人工确认一行。

**为什么不用 B站分区判**：实测地理科普视频被归到 `tid 21`（日常），且 view 接口 `tname` 常为空——**分区信号对本场景不可靠**，只用 title/desc/UP主 三个信号。

**代价不对称**：漏切（仍是 whisper）= 维持现状；误切 = 慢 3-10 倍 + 英文质量下降。所以灰区（score 0 或 low）一律判 whisper。

### 3e. 自迭代验证机制（遇到实际任务时顺带验证，逐步把推断升级为实测）

白名单里只有部分 UP 主经过实测，其余是标题推断。**不专门跑验证任务**，而是在每次真实处理视频时顺带完成，成本趋近于零。

**闭环流程**：

```
--classify 提示 verified=false（待验证）
        ↓
按建议跑完视频（正常任务，不为验证而跑）
        ↓
顺带做一次专名核对（见下方三种方式，选最便宜的）
        ↓
结论写回：① 白名单依据类型改「实测验证」+ 附 BV 例证
          ② 追加一行到 references/engine_verify_log.txt
        ↓
下次同 UP 主 classify 显示 verified=true，不再提示
```

**三种验证方式（按成本从低到高，优先用低成本的）**：

| 方式 | 成本 | 适用 | 操作 |
|---|---|---|---|
| **字幕对照** | 零额外 ASR | 视频**有官方 AI 字幕** | 拿字幕与转写对比专名（字幕是人工/官方 AI 结果，可信） |
| **常识核对** | 零 | 任何 | 读素材包正文，凭常识判断专名是否被错写（"险性债务"→明显应为"隐性债务"） |
| **双跑抽样** | 约 1-2 分钟 | 存疑、且上面两种定不了 | **只跑前 60-90 秒**（不全片），whisper 与 Nano 各一次对比 |

> 双跑抽样只跑片段——全片双跑要十几分钟，片段足够看出同音混淆模式（whisper 的典型错误是"隐性→险性/隐姓"这类同音替代，前 90 秒就会暴露）。

**判错的处理**：**不删原记录**，改判并留修正说明（保留可追溯）。例：某财经类 UP主原判 whisper，实测后改判 Nano，白名单与日志都保留改判痕迹与依据。

**触发提醒**：`--classify` 对未实测的 UP 主会返回 `verified: false` + `verify_hint`，我看到就应在本次任务收尾时顺带核对。

**当前验证进度**：

发布仓库不含作者的私人白名单（那是个人观看记录）。下面给出**类别 → 建议引擎**的通用结论，可直接作为你自己的白名单起点：

| 内容类别 | 建议引擎 | 依据 |
|---|---|---|
| 历史 / 地理 / 地方志（地名、朝代、古迹密集） | funasr-nano | 实测：Nano 专名 10/11 vs whisper 2/11 |
| 财经 / 政经（机构名、政策术语密集） | funasr-nano | 实测：Nano 债务类术语全对，whisper 同音错 5 处 |
| 国际政经（国名、机构、人名密集） | funasr-nano | 标题归类 |
| 泛科技 / AI 产品（英文产品名、技术术语多） | whisper | 标题归类；Nano 英文 token 易粘连 |
| 方法论 / 社会议题（非专名密集） | whisper | 标题归类；whisper 快约 20x |

### 3c. AI 侧自动切换规则（执行时由我判断并带参数，脚本不会自动切）

脚本本身**不做内容类型识别**——`--engine` 必须显式传。切换决策由**我在接收任务时**按下面规则判断，然后把参数写进命令。

**命中任一信号 → 自动加 `--engine funasr-nano`（+ 热词）**：

| 信号类型 | 具体清单（基于现有 138 份 raw 实测归类） |
|---|---|
| **UP主白名单**（历史/地理/政经） | 命中 `references/engine_up_whitelist.txt` 中已登记且经实测的 UP主 |
| **标题关键词** | 省/市/县/区地名（肇庆、怀集、湛江、茂名、海南、东南亚…）、朝代与历史人物（宋徽宗、赵佶、明朝、客家…）、政经实体（霍尔木兹、G7、财政、化债…）、系列名（"XX 里的中国"、"XX 为什么"） |
| **内容特征** | 同一段口播里出现 ≥2 个陌生专名；人物/地名/机构密集 |

**明确不切（维持 whisper）**：

| 情形 | 原因 |
|---|---|
| 视频有**人工 CC 字幕** | 字幕优先，切引擎无效（字幕路由在 ASR 之前） |
| 视频只有 **AI 字幕** 且专名密集 | **`--force-asr` 强制本地转写**，不要信回译字幕（见「3. 路由逻辑」的 AI 字幕警告） |
| 泛科技 / AI 产品向、方法论类 | 英文产品名/术语多，Nano 英文 token 会粘连；whisper 20x 更快 |
| 英文歌、纯英文内容、音乐 MV | Nano 对外语不占优 |
| 快速批量初筛、只想看个大概 | 速度优先 |

> **归类以内容主体语言为准，不是按"领域标签"**：某财经类 UP主 虽涉 AI 与政治，但叙述主体是**中文财经专名**（化债、财政体系、大厂资本）→ 判 **Nano**；某科技类 UP主 主体是**英文 AI 产品名**（OpenClaw、WorkBuddy、Agent）→ 判 **whisper**。判据是"专名用什么语言写"，不是"讲哪个领域"。

**切换时同步带热词**（否则只发挥一半效果）：从视频标题、简介、UP主名抽取 20-50 个专名写入临时热词文件（去重、硬上限 80），再用 `--hotwords` 传入。

**执行模板**：
```bash
# 命中信号 + 无字幕
bili_asr.py <链接> --engine funasr-nano --hotwords <临时热词文件> --out <素材包>
# 命中信号但不确定有无字幕：先 --only-meta 看一眼，或直接加 --force-asr 强制走 Nano
```

**误切的代价**：慢 3-10 倍 + 英文内容质量下降；**漏切的代价**：维持 whisper 现状（不会更差）。拿不准时按"不切"处理，或在评论/标题明显是地名密集时再切。

### 4. 阅读素材包
用 Read 读取 `out` 路径的 Markdown：基本信息、简介、分P、章节、带时间戳正文、热评 Top20。
**必须基于正文全文总结，不得只依赖标题/简介/评论。**

### 5. 生成知识库条目

产物**只有一种格式**：`references/knowledge-entry-template.md` 定义的 14 节知识库条目。

写入 `raw/<发布日>_<完整标题>_<UP主>_纪要.md`。

不区分"简单总结"和"存知识库"——用户存这些文件就是为了能搜到，精简版省下的那点篇幅，代价是半年后检索不到。用户明确要求极简摘要（如"三句话说完"）时，直接在回复里答，不落盘。

**派发子代理前的两个硬动作**（漏一个就可能产出残次结构）：
1. **读 `references/knowledge-entry-template.md` 原文**，或把「输出结构（14 节）」里的骨架原样抄进 prompt。**禁止凭记忆复述章节列表**——2026-09-02 就是因为凭记忆写 prompt，连续两批 46 份退回 6 节旧模板。
2. 在 prompt 里写明：写完必须跑 `python scripts/verify_structure.py <产物路径>`，不通过就改到通过。

**子代理返回后**：不要只看它的完成报告，跑一次结构校验确认——报告说"已完成"和实际合规是两回事。

**命名规则（硬规则）**：`<发布日>_<完整标题>_<UP主>_纪要.md`
- 发布日取视频发布日，不是处理日
- 标题原文照抄，只剔除 Windows 非法字符 `\ / : * ? " < > |`；全角标点（，？【】）**保留**
- **`/` 一律替换为 `、`（不是删除）**——保住可读性，且反查 BV 时 norm 需同时忽略 `/` 与 `、` 才能对上（实测坑：湖森堡系列标题含 `Cola/codex/Claude Code/workbuddy`）
- 队列模式下直接用 `--note-name <BV号>` 拿标准文件名，别自己拼
- 正确示例：`2026-01-05_【伯爵】借债，卖官，罚款，古代统治者有哪些增收手段？一期讲清古代非税收入_河畔的伯爵_纪要.md`

**frontmatter 写入坑**：`tags` / `entities` 数组内**禁止嵌套方括号**（如写 `[原文疑似]` 这种值）——`verify_structure.py` 的非贪婪正则 `\[(.*?)\]` 会在嵌套处截断，导致计数偏少、校验误报。存疑标注改用全角括号（`（原文疑似）`）或从数组移出。

### 6. 内容要点写法

见 `references/knowledge-entry-template.md` 第四节：**能用表格就不用散文**。表格每格都是可检索单元，散文里的句子搜不到。

---

## 输出结构（14 节）

完整结构见 `references/knowledge-entry-template.md`，必须按序写全。

⚠️ **2026-09-02 事故**：连续两批（共 46 份）产物退回 6 节旧模板——派发时凭记忆写 prompt，没去读模板文件。教训：**派发子代理前，把下面的骨架原样抄进 prompt，不要凭印象重写。**

⚠️ **2026-09-06 事故**：腾讯首席 AI 科学家「姚顺雨」被反复误识为「尧舜禹」，错误写法一路带进正文和 frontmatter entities，把检索入口堵死——AI 看到「尧舜禹」以为是合法历史词，不会起疑。教训：**派发子代理前，必须把 `references/asr_glossary.txt` 中本批相关的条目原样抄进 prompt**，并且生成完成后**强制跑一次** `scripts/check_glossary.py --dir <raw>` 兜底（详见「专名误识硬规则」章节）。

| 节 | 内容 |
|---|---|
| frontmatter | 元数据 + **tags** + **entities** + confidence + review_by |
| 一 | 检索入口表：这条知识能回答什么问题 |
| 二 | 核心结论（一句话 + 3–5 条带时间戳要点） |
| 三 | 关键实体表（人物 / 产品 / 模型或制度） |
| 四 | 内容要点（表格优先，带时间列） |
| 五 | 章节脉络表 |
| 六 | 反共识观点（单列，不埋正文） |
| 七 | 时间线 |
| 八 | 关键决策与结论 |
| 九 | 金句 |
| 十 | 待验证清单（判断 + 依据 + 验证时点 + 方式） |
| 十一 | 术语表 |
| 十二 | 信息完整性 |
| 十三 | 相关链接 |
| 十四 | 更新记录 |

**可直接抄进 prompt 的骨架**（照抄，别自行精简）：

```markdown
---
title: <可读标题，概括主张而非照抄视频标题>
date: YYYY-MM-DD
type: <播客访谈纪要/知识科普/评论解说/教程演示/圆桌对谈>
source: <平台 + BV号>
source_url: <链接>
duration: <时长>
speakers: <主讲人/嘉宾>
confidence: 高/中/低（必填理由）
tags: [6-10 个，领域词与具体词混合]
entities: [8-12 个，人物/组织/产品/地名，写全名带别名]
created: YYYY-MM-DD
review_by: YYYY-MM
status: 待复核
---

# <标题>
> 元数据块：来源、时长、编号、转写方式、嘉宾/主讲人

---
## 一、这条知识能回答什么问题
## 二、核心结论
## 三、关键实体表
### 3.1 人物—角色—关键信息
### 3.2 产品—归属—状态
### 3.3 模型/项目—参数—状态
## 四、内容要点        # 能用表格就不用散文
## 五、章节脉络
## 六、反共识观点
## 七、时间线
## 八、关键决策与结论
## 九、金句
## 十、待验证清单      # 判断+依据+验证时点+验证方式
## 十一、术语表
## 十二、信息完整性    # 转写覆盖 + ASR误识对照表 + 证据强度问题
## 十三、相关链接
## 十四、更新记录
```

**tags / entities 决定半年后能不能搜到**，填写粒度见模板。写完全文再回头补这两项——只有通读完才知道这条真正讲的是什么。

---

## 生成后必做：结构校验

产物落盘后立即跑，不要等用户发现：

```bash
python scripts/verify_structure.py raw/<新生成的文件>.md
python scripts/verify_structure.py --dir raw     # 全库体检
```

校验四项：frontmatter 齐全、tags 6–10 / entities 8–12、14 节齐全、内容要点已表格化。**任一项不通过就退回重写，不要带着结构缺陷交付。**

与 `verify_coverage.py` 分工：本脚本查「结构对不对」，`verify_coverage.py <原> <新>` 查「增强时有没有遗失」。重写时两个都必须过。

---

## 批量队列处理

### 主入口：WorkBuddy 资料库在线表

用户批量收集链接时用这套。台账是资料库里的一张在线表：

| 项 | 值 |
|---|---|
| 表名 | B站视频转录台账 |
| database_id | `<YOUR_DATABASE_ID>` |
| 位置 | 我的文档 › BILIBILI视频内容音频转录 |
| 访问 | <你的资料库在线表链接> |

字段（12 列）：视频链接 / BV号 / UP主 / 视频标题 / 时长 / 状态 / 转写耗时(秒) / 素材包 / 纪要 / 处理时间 / **IMA转存** / 备注

两个状态列职责不同，不要混用：

| 列 | 含义 | 取值 |
|---|---|---|
| 状态 | 本地转写进度 | 待处理 / 已转写 / 已完成 / 失败 / **重复** |
| IMA转存 | 上传 IMA 知识库的进度 | 未转存 / 已转存 / 失败 / 不适用 |

写入 select 传选项文本即可。「不适用」用于无需上传的场景（重复条目、用户明确只要本地存档）。新记录默认「未转存」，上传成功后改「已转存」。

**查重（必须先做）**：新链接进入处理前，先把它的 BV号 与表中所有「已转写 / 已完成 / 重复」行的 BV号 比对。命中则状态写「重复」、备注写明与哪条重复，**不再本地转写**。

用 `scripts/library_queue.py` 一次跑完查重 → 补信息 → 转写 → 回写：

```bash
PYTHONPATH= ~/.workbuddy/binaries/python/envs/whisper/Scripts/python.exe \
  "~/.workbuddy/skills/bilibili-video-summary/scripts/library_queue.py" \
  --token <op_token> [--meta-only] [--limit N] [--model <模型>]
```

| 参数 | 作用 |
|---|---|
| `--token` | 资料库凭证，由 `connect_open_platform` 取得，**有效期 30 分钟**，过期重取 |

⚠️ **token 过期的症状不是鉴权报错，别误判数据丢了**（2026-09-06 踩坑）：
- `library_queue.py --finish` 报 `{"error": "未找到 BVxxxxxxxxx"}` —— BV 明明在台账里，其实是 token 过期导致查询返回空
- `query_database_record.py` 报 `code=NETWORK_ERROR` 或「台账总行数 0」
- 遇到这两种，**先重取 token 再重试**，不要去改台账、不要以为记录被删了

⚠️ **两个脚本的 token 传参方式不同**：`library_queue.py` 用 `--token xxx`；`query_database_record.py` / `batch_update_database_records.py` / `batch_delete_database_records.py` 必须 **stdin 管道传**——`echo "<token>" | ... --token-stdin`，直接 `--token xxx` 会返回 `code=AUTH_REQUIRED; msg=token 缺失或无效`，且 `--help` 也同样报这个错（不是脚本坏了）。另外 `query_database_record.py` 没有 `--out` 参数，导出用 shell 重定向 `> file.json`。
| `--meta-only` | 只查重 + 补元信息，不转写（几秒一批） |
| `--limit N` | 本次最多处理 N 条 |
| `--up <UP主>` | 只处理指定 UP主（按「UP主」列精确匹配），适合按系列分批 |
| `--engine whisper\|funasr-nano` | 透传给 `bili_asr.py`；**不传则队列跑 whisper**，专名密集批次要显式指定 |
| `--hotwords <文件>` | 透传给 `bili_asr.py` 的热词文件路径（仅 Nano 生效） |
| `--raw-dir` | **纪要**落盘目录，默认 `<RAW_DIR>` |
| `--cache-dir` | **素材包**缓存目录，默认 `<CACHE_DIR>` |
| `--note-name <BV号>` | 只输出该 BV 的标准纪要文件名，不碰台账 |
| `--finish <BV号>` | 收尾模式：回填纪要 + **归档素材包到 bili_subs 成功后**才置状态「已完成」；归档失败返回 error、不动台账 |
| `--summary <路径>` | 配合 `--finish`，写入「纪要」列 |
| `--ima <状态>` | 配合 `--finish`，写入「IMA转存」列 |

输出 JSON 含 `duplicates` / `processed` / `failed` 三组；`processed[].note_name` 直接给出标准纪要文件名。

标准节奏：先 `--meta-only` 跑一遍（用户确认视频对不对）→ 再不带参数跑转写（素材包落缓存，stderr 打出建议纪要名）→ AI 读素材包写纪要到 `raw/`、传 IMA → `--finish` 回填并把素材包**归档**到 `bili_subs/`。

**补改已完成的单行字段**（例：IMA 未连上先落本地、事后补传要改「IMA转存」）：`--finish` 只能作用于未完成行，对状态已是「已完成」的行会报「未找到 <BV号>」。此时改用资料库 database 模块按 record_id 直改，不要重跑转写：

```bash
echo "<token>" | python3 ".../library/database/batch_update_database_records.py" --token-stdin \
  --database-id "<YOUR_DATABASE_ID>" \
  --records '[{"record_id":"<rid>","properties":{"IMA转存":{"select":"已转存"}}}]'
```

```bash
# 收尾（纪要用标准名；路径一律正斜杠，Git Bash 会把 \ 转成 // 污染台账字段）
... library_queue.py --token <op_token> --finish BVxxxxxxxxx \
    --summary '<RAW_DIR>/2026-01-05_完整标题_UP主_纪要.md' \
    --ima 已转存
# 输出里 archived 列出归档后的素材包路径，台账「素材包」列自动更新为归档路径。
# 素材包不在缓存也不在归档目录（或移动失败）时返回 error，台账保持原状态——别把「已完成」当归档成功的保证。
```

**素材包归档保留，不删除**：转写全文是误识修正的唯一依据（拿字幕原文逐处对照，不凭上下文猜）。落 `.workbuddy/cache/bili/`（Obsidian 不索引）→ `--finish` 后自动挪到 `.workbuddy/cache/bili_subs/` 长期保留，详见下方「产物去向」。

### 本地 CSV 队列（离线或批量场景）

`scripts/process_queue.py` 维护本地 CSV 台账，适合一次性批量处理。默认路径 `<CSV_PATH>`，可用 `--csv` 覆盖：

```bash
PYTHONPATH= ~/.workbuddy/binaries/python/envs/whisper/Scripts/python.exe \
  "~/.workbuddy/skills/bilibili-video-summary/scripts/process_queue.py" <参数>
```

| 参数 | 作用 |
|---|---|
| `--init` | 初始化台账（含示例行） |
| `--status` | 查看队列状态（只读） |
| `--meta-only` | 只补元信息，不转写（每个 1–2 秒） |
| 无参数 | 补元信息 + 转写 |
| `--limit N` | 本次最多处理 N 条 |
| `--bvid BVxxx` | 只处理指定视频 |
| `--model <模型>` | 指定转写模型 |
| `--csv <路径>` | 指定其他台账 |

**列职责**

| 列 | 填充方 |
|---|---|
| 视频链接 | 用户粘贴 |
| 序号、BV号、UP主、视频标题、时长 | 脚本自动 |
| 状态、转写耗时(秒)、素材包、处理时间 | 脚本自动 |
| 纪要 | 转写完成后由 AI 生成并回填 |
| 备注 | 用户 |

**状态流转**：空 / 待处理 → 已转写 → 已完成；异常记为 `失败:<原因>`，失败行不会被重复处理。

**产物目录**（台账同级）：`transcripts/` 放素材包，`notes/` 放纪要。CSV 为 UTF-8 BOM 编码，Excel 直接打开不乱码。

**完整流程**：用户粘链接 → 跑 `--meta-only` 预览（确认视频对不对）→ 跑转写 → AI 读素材包写纪要，回填「纪要」列并把状态改为已完成。

---

**`review_by` 字段**：根据内容中最远的待验证判断设定，通常 3–6 个月。

---

## 广告过滤

视频里常有带货口播（拼多多、神奇小鹿、萌芽家这类），**这些内容不得写入纪要**。

两级处理：

1. **脚本预标记**：`bili_asr.py` 用 `references/ad_keywords.txt` 扫描转写段落，命中的段落加 `[广告?]` 前缀，并在素材包末尾生成「疑似广告段落」表格（时间戳 + 命中词 + 内容）。
2. **AI 最终判断**：生成纪要时跳过所有带 `[广告?]` 的段落，不进核心结论、不进内容要点、不进金句。脚本只做提示，判断权在 AI——词表漏掉的广告口播同样要剔除。

词表可增删：直接改 `references/ad_keywords.txt`，每行一个词，`#` 开头为注释，改完立即生效。当前 70 词，分四类：

| 类别 | 举例 | 信号强度 |
|---|---|---|
| 带货话术 | 优惠券、小黄车、下方链接、感谢金主 | 强，命中基本可判定 |
| 圈内暗号 | 上手体验、一拍即合、工商、本期由 | 强，UP主提示含商单的约定说法 |
| 播客/知识区高频广告主 | 饿了么、斯维诗、雅诗兰黛、始祖鸟、龙角散、华夏基金 | 弱，需复核上下文 |
| 内容社区常见投放品牌 | 拼多多、麦当劳、蜜雪冰城、米博、独特艾琳 | 弱，需复核上下文 |

**撞车词必须写产品全称**：品牌名若与生活常用词、人名、书名或学科术语重合，只写全称或产品名。已踩过的坑——写「夸克」会把物理科普里的夸克误标，写「苏菲」会把《苏菲的世界》误标；正确写法是「夸克App」「苏菲卫生巾」。新增品牌前先想清楚它会撞什么。

**注意**：广告段落仍保留在素材包正文里（带标记），保证生成阶段可追溯；过滤只作用于纪要生成阶段。素材包删掉后，可追溯性由纪要「信息完整性」章节里的「广告段落处理」条目承担。

**纪实/产业类系列复核原则**：这类 UP主 正文大量提及品牌、产品、产业（拼多多、麦当劳、当地特产等），命中弱信号词属正常。AI 生成纪要时必须逐段复核上下文——叙述城市产业/事件/特产 → 属于节目内容，**保留**并写入纪要；只有 UP主 本人的带货口播（念链接、优惠券、感谢金主、推荐购买）才剔除。已实测：某纪实系列 24 条全量处理中，所有 `[广告?]` 标记段落经复核均正确区分（产业叙述保留、真口播剔除），无大段误删。

## 产物去向

**纪要长期保留；素材包归档，不再删除。**

| 产物 | 去向 | 生命周期 |
|---|---|---|
| 素材包（转写全文 `bili_<BV>.md`） | `.workbuddy/cache/bili_subs/` | **归档保留**（2026-09-02 起） |
| 纪要 / 知识条目 | `<RAW_DIR>` | 长期保留，命名见第 5 步硬规则 |
| 纪要副本 | IMA「B站视频转录」kb_id `<YOUR_IMA_KB_ID>` | 上传一次即归档；内容变更时用 REPLACE 覆盖 |

**为什么素材包改为保留**（这条踩过两次）：
- 2026-09-01 修 ASR 误识时，发现 32 条素材包已被 `--finish` 删除，只能靠字幕重跑一遍——当时登录态还没配，等于重做转写。
- 素材包是**误识修正的唯一依据**（要拿字幕原文逐处对照，不是凭上下文猜）。删掉就等于放弃修正能力。
- 体积代价可忽略：单份 20–150 KB，100 份约 10 MB。

归档操作：**`--finish` 已内置归档**（2026-09-05 起）——脚本自动把 `cache/bili/bili_<BV>.md` 挪到 `cache/bili_subs/`，并把 index.json 的 `transcript` 指向新路径，台账「素材包」列同步更新为归档路径。素材包已在归档目录内的（历史批次）自动识别为已归档，不重复移动。直接跑 `--finish` 即可，不需要手工 `shutil.move`。**归档失败（素材包缺失 / 移动出错）时不置「已完成」**：输出 error + 原因，台账保留原状态，处理后重跑。

⚠️ 归档目录里存在**两种前缀**：`sub_BV*.md`（2026-09-01 批次，18 份）和 `bili_BV*.md`（后续批次）。统计数量时只 glob 一种会漏掉另一批。

IMA 通道不可用时**降级**：只落本地，把 IMA转存 保持「未转存」并在回复中说明「IMA 未连接，本次仅本地归档」，核心流程不中断。上传成功后把该行 IMA转存 改为「已转存」；失败改「失败」并在备注写原因。**只有看到「已转存」才算归档完成**，不要凭推测标注。

⚠️ **禁止"中性化处理"（2026-09-08 事故）**：子代理/编译员遇到敏感人名、组织名、政治与地缘表述时，容易自作主张改写成中性说法（把"朱镕基"写成"时任国务院总理"、把"法轮功"写成"某组织"、把具体姓名改成"某位校友"）。这**违反**如实记录原则，且直接毁掉可检索性。正确做法：**只区分事实与判断、标明立场归属（"此为 UP 主观点"），绝不淡化、替换或删名**。ASR 失真就保留原词 + 在第十二节列对照表。派子代理写纪要时，必须在指令里显式写这条禁令。

### IMA 上传三步走

**只传纪要，素材包不传 IMA**——素材包只在本地归档（`bili_subs/`），供误识修正回查；传上去会污染检索结果，且 IMA 没有删除接口，传错了只能手动清理。

`add_knowledge` 只认 `media_id`，字节流要自己推到 COS。完整链路：

```bash
# 1) 取凭证：mcp__ima-mcp__create_media(
#      knowledge_base_id, file_name, file_ext, file_size, content_type)
#    返回 media_id + cos_credential；content_type 必须精确（md → text/markdown）

# 2) 推字节流（--cred 收整个返回 JSON 的文件路径，也收 JSON 字符串）
PYTHONPATH= ~/.workbuddy/binaries/python/versions/3.13.12/python.exe \
  "~/.workbuddy/skills/bilibili-video-summary/scripts/ima_cos_upload.py" \
  --cred <凭证JSON文件路径> --file <本地文件>

# 3) 入库：mcp__ima-mcp__add_knowledge(
#      knowledge_base_id, media_id, duplicate_name_strategy=..._REPLACE)
```

**批量上传（多条一次跑）**：把多个 create_media 的返回按 `{"file","media_id","cos_credential"}` 拼成 JSON 数组文件，一条命令跑完：

```bash
PYTHONPATH= ~/.workbuddy/binaries/python/versions/3.13.12/python.exe \
  "~/.workbuddy/skills/bilibili-video-summary/scripts/ima_cos_upload.py" \
  --batch <凭证数组JSON文件>
```

- 数组元素：`{"file": <本地纪要路径>, "media_id": <create_media 返回的 media_id>, "cos_credential": <原样>}`
- 输出逐条 ok/status，退出码非 0 表示有失败项
- 凭证抄写错误会返回 `InvalidAccessKeyId`，重跑 create_media 再补

**台账收尾（批量）**：22 条级队列不要逐条 `--finish`（每次要重读台账，token 只活 30 分钟）。写一次性 Python 循环：读台账 → 对每条非「已完成」行回填（状态=已完成、纪要=标准路径、IMA转存=已转存，攒批调 `update()`）→ **归档素材包**（把 `cache/bili/bili_<BV>.md` 挪到 `cache/bili_subs/` 并更新 index.json 的 `transcript`，行为与 `--finish` 一致），**不要删除**。单条收尾直接 `--finish` 即可。

踩过的坑：
- `bucket_name` 常**已带 appid 后缀**，再拼一次 `-{appid}` 会 404 `NoSuchBucket`。脚本内置多 host 候选（`bucket.cos.region` → `bucket-appid.cos.region` → `custom_domain`），逐个试并把每次失败打到 stderr。
- `custom_domain`（`*.image.myqcloud.com`）是图片 CDN，PUT 必 403 且可能不解析，排最后。
- 报 `InvalidAccessKeyId` = 凭证抄错。**不要手抄长 token**：把 `create_media` 整个返回原样写进临时文件再喂脚本，抄错一位就 403；用完立即删（含密钥）。
- 只传 `cos_credential` 子节点会丢 `media_id`，脚本先取顶层 `media_id` 再下钻。
- 上传的 `file_name` 用与本地一致的标准名，IMA 里才能对得上。
- **工具「已连接」≠ 可用**：桌面面板显示已连接、`connector-status` 显示 connected，都不代表工具暴露给 agent。唯一判定标准是 `ToolSearch` 搜 `mcp__ima-mcp__create_media` 能否搜到。搜不到就是没暴露，别反复重试，直接降级本地归档。
- **当前会话内启用连接器不会刷新工具索引**（2026-09-04 实测）：会话启动时 IMA 未启用 → 索引快照里没有 `mcp__ima-mcp__*`；会话中途在面板启用后，`connector-status` 会变成 connected、`connector-states.json` 里 `enabled: true` 也确实写入了，但 **deferred tools 索引不刷新**，`ToolSearch` 依旧搜不到、直接 `DeferExecuteTool` 调用报 `not found in the deferred tools index`。此时改配置文件无济于事，**唯一解法是开一个新会话**（新会话启动时索引会重新加载 IMA 工具）。所以批量上传前应先确认工具可用，而不是假定「用户说连好了就能用」。

- ⚠️ **但开新会话也不保证成功**（2026-09-07 修正）：新会话启动时 IMA 工具仍未进索引——查 `connectors/default/mcp.json` 里 `connector:ima-mcp` 是 `disabled: true`。即**新会话只在你启用过的那个配置下才加载得到**。`disabled` 字段才是真相，不是面板、也不是"是不是新会话"。
  排查顺序（按顺序查，别跳）：
  1. `ToolSearch` 搜 `mcp__ima-mcp__create_media` —— 搜到才算可用（唯一判定标准）
  2. 搜不到 → 查 `~/.workbuddy/connectors/*/mcp.json` 里 `connector:ima-mcp` 的 `disabled` 字段。注意有多个 workspace 配置目录，**逐个查**：`disabled: true` 的那个生效时就用不了
  3. 确认是 disabled → 去连接器管理页点「信任/启用」→ **再开新会话**
- **上传受阻时不要干等**：把待传清单写进 `.workbuddy/cache/ima_pending_overwrite.json`（文件路径 + 大小 + kb_id + REPLACE 策略 + 步骤），下次会话可用时直接照着跑，不用重新排查覆盖面。
- **REPLACE 前先反查云端 title**（2026-09-07 实测）：同名判定靠云端 `title`，且**含 `.md` 扩展名**。用 `search_knowledge` 搜关键词确认 title 与本地文件名逐字一致，不一致就会新建一条而不是覆盖，IMA 又没删除接口。
- **`search_knowledge` 单次返回 ~117KB**（几乎是全库条目列表，不是命中片段）：会自动落盘成 txt，**别直接读**，用 python 从落盘文件里抽 `title`/`media_id`。
- **覆盖验证挑小的那条抽查**（2026-09-07 实测）：`fetch_media_content` 拉体积小的那份（如 42KB）确认 corrected 词已生效即可，别拉 125KB 的——内容直接进上下文，代价太大。
- **覆盖上传的验证标准与首次上传不同**：首次上传看 `knowledge_total_size` 传 N 条应 +N；**覆盖同名条目时条目数不变**，需改看单条 size 字节数变化或抽查条目内容确认已更新。
- **`create_media` 偶发返回空**：`{"media_id":"","cos_credential":null}`，不是参数错误，原样重试第二次必成。别改参数瞎试。
- **media_id 抄错不报错**：长十六进制串手写进 batch JSON 时多/少一位，COS 推流照样 200，直到 `add_knowledge` 才指向错误对象，且此时已无法察觉。校验判据用「**以知识库 id `<YOUR_IMA_KB_ID>` 结尾 + 长度 ≥80**」；~~全长 84 字符~~的说法是错的，**实测全长 90**（`markdown_` 9 + 32hex + `_` 1 + 32hex + kb_id 16），按 84 校验会把所有合法 id 判为非法。
- **批量覆盖用「索引配对」，别手工拼 batch JSON**（2026-09-04 实测 63 份）：先把待传清单按固定顺序落成 `ima_rows.json`（含 name/size/path），`create_media` 的返回按同一顺序写成 `ima_cred_<n>.json`，再用脚本按索引号配对拼 batch。这样 media_id 与本地文件不可能串位——手工拼时一旦错位，推流照样 200，只能等 add_knowledge 才发现，且已无法挽回。
- **覆盖的并行节奏**：`create_media ×8` → 写 8 个凭证 → `--batch` 推流 → `add_knowledge ×8`；推流命令与下一批的 `create_media ×8` 同一轮发出，流水作业。63 份实测约 21 分钟。
- **凭证用完立即删**：`ima_cred_*.json` 含 `secret_key`。63 份清完一次性 `rm -f`，Windows 下 `rm` 一次删 60+ 个文件可能被 SIGTERM 打断，**分 5 批每批 ≤16 个**稳妥。
- **批量节奏（32 条实测）**：`create_media` 逐条取凭证攒够 8 条 → 写进一个 JSON 数组跑 `--batch` → `add_knowledge` 逐条入库。凭证有效期 12 小时（`expired_time - start_time = 43200`），攒批不超时。
- **校验闭环**：`get_knowledge_base_list` 看 `knowledge_total_size` 增量，传 N 条应正好 +N。这是唯一可信的完成证据，不要用「调用没报错」当证据。
- IMA 没有删除接口，传之前先确认文件对不对。

## 只增不减（硬规则）

**对已有产出做增强时，原内容必须 100% 保留，只追加新章节，不重排框架。**

这条规则踩过坑：把纪要升级为知识库条目时重排了章节，导致章节脉络表、两个完整小节（各含 7 条要点）、关键决策与结论全部丢失，用户明确指出内容遗失。

具体要求：
- 原文件的章节标题、时间戳、表格、金句说明文字，逐项保留，不精简、不合并、不改写措辞
- 新增要素以**独立章节追加**，不插入或替换原有结构
- 原有小节的编号和顺序保持不变
- 增强后文件体积应**大于**原文件（+50% 以上属正常），若变小说明发生了删减

## 覆盖校验

**重写或增强已有条目后必须执行**，用脚本客观比对，不靠肉眼。初次生成不需要。

```bash
cd <工作区>
PYTHONPATH= ~/.workbuddy/binaries/python/envs/whisper/Scripts/python.exe \
  "~/.workbuddy/skills/bilibili-video-summary/scripts/verify_coverage.py" \
  "<原文件>" "<新文件>"
```

校验三项：
1. **时间戳覆盖** —— 原文件所有 `hh:mm:ss` 是否都在新文件中（应为 0 缺失）
2. **章节标题覆盖** —— 原文件所有 `##`/`###` 标题是否都在新文件中
3. **体积变化** —— 新文件字符数应大于原文件

任何一项不过，说明发生了遗失，必须补回后重新校验。

---

## 实测性能（NVIDIA GPU 参考）

11:30 视频 = 689.7s 音频，turbo @ CUDA：

| 环节 | 耗时 |
|---|---|
| 模型加载 | 4.0s |
| 音频下载（m4a，不转码） | 2.5s |
| ASR 转写 | 33.7s |

| 模型 | 转写耗时 | 实时倍数 | 1h 视频推算 |
|---|---|---|---|
| `large-v3-turbo` | **33.7s** | 20.5x | **~3.0 min** |
| `large-v3` | 106.6s | 6.5x | ~9.2 min |

1:39:28 长视频实测：下载 44s + 转写 243s = **4 分 48 秒**（24.6x 实时）。

### 批量处理：整批只加载一次模型

`library_queue.py` 分两阶段执行，**阶段二用 `--batch-file` 把整批转写合并成一次调用**：

| 阶段 | 动作 | 模型加载次数 |
|---|---|---|
| 一 | 逐条查重 + 补元信息（view 落 `~/.workbuddy/cache/bili/meta/`） | 0（不加载） |
| 二 | `--batch-file` 一次性转写全部（view 直接命中阶段一缓存） | **1** |

省下的是 (N-1) × 4s 的模型加载 + 每次的进程启动开销 + 阶段二重复请求 view 接口的往返。10 条批量约省 40–50s。

也可直接调用批处理模式：

```bash
# items: [{"url": "...", "out": "路径.md"}, ...]
PYTHONPATH= HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_SYMLINKS=1 HF_HUB_DISABLE_XET=1 \
  ~/.workbuddy/binaries/python/envs/whisper/Scripts/python.exe \
  "~/.workbuddy/skills/bilibili-video-summary/scripts/bili_asr.py" \
  --batch-file <items.json> --model large-v3-turbo
```

输出 `{"ok": true, "count": N, "results": [...]}`，单条失败记在 `results[].error`，不中断整批。

### 音频不转码

yt-dlp 直接落 B站原生 m4a(AAC)，PyAV 可直接解码，省掉 ffmpeg 转码。实测下载环节从 3s 降到 2.5s，且不再依赖 `--audio-format` 转换。

### 已验证不可用的提速方案

**`BatchedInferencePipeline` 不要用。** 实测转写快 2.05x（14.6s vs 30.0s），但输出出现幻觉——凭空多出「请不吝点赞 订阅 转发 打赏支持明镜与点点栏目」，且错别字明显增多。2 倍提速是拿质量换的，不值。

### 评估后不采用的优化（有实测依据，不逐一试行）

| 优化方向 | 结论 | 依据 |
|---|---|---|
| 降低 `beam_size`（5→1/2/3） | **不做** | 调参收益要拿同段音频逐档比误识率才知道，属"测了才值不值"。已有 `BatchedInferencePipeline` 拿速度换质量的翻车教训（幻觉+错字），说明本场景对转写质量敏感。真出现术语密集、误识率高的长视频时按需临时用 `large-v3`，而不是全局调解码参数 |
| 音频降码率（yt-dlp 改低码率格式） | **不做** | 下载环节实测仅 2.5s/条，降到 96–128kbps 省不了多少时间；且 B 站部分视频只有单一音轨，格式筛选可能取不到音频导致失败。省时间要从 GPU 侧找，不是网络侧 |
| 并行下载 + 单 GPU 转写流水线 | **不做** | 实测下载 2.5s（短视频）/ 44s（1:39 长视频），转写 33.7s/条。串行时 GPU 只在长视频下载期空等，短视频批量收益极小；复杂度高（多线程队列 + 失败重试 + 磁盘清理），与收益不成比例。批量已做到"整批只加载一次模型"，先吃下这个就够了 |

### 分段耗时与元信息缓存（2026-09-05 起）

返回 JSON 增加 `timing.meta_sec`（view/字幕抓取耗时），ASR 结果拆分 `model_load_sec`（模型加载，仅首条非 0）与 `asr_sec`（纯转写，不再混入加载时间）——`asr_sec` 语义沿用，台账「转写耗时(秒)」字段不受影响。

元信息带 24h 本地缓存（`~/.workbuddy/cache/bili/meta/<BV>.json`）：批量队列先 `--meta-only` 跑一遍补元信息，随后转写阶段直接命中缓存，不再重复请求 view 接口。

`--only-meta` 只请求 view 接口：不再顺带抓章节/评论，预扫描从 3-4 次请求降到 1 次。要看章节/热评走完整转写。

## 精度实测结论
中文口语场景下，**turbo 与 large-v3 的准确率差距远小于 3 倍的速度差**：
- large-v3 正确、turbo 错：生态位、姓魏的商人、小浣熊、沈殿霞
- **两者共同短板**：人名、品牌名、金额数字（"安藤百福"→"安等/安登百福"；"一角五分"→"一脚五分/195分"）
- 结论：**默认 turbo**。术语密集或需逐字引用时才用 large-v3；数字与专名在产出中一律标注 `[原文疑似]`，并在「信息完整性」列出误识对照表。

### 专名误识硬规则（2026-09-06 起）

**最危险的一类误识：误识结果本身是合法中文词**——AI 读到时不会起疑，错误写法会一路带进正文、带进 frontmatter 的 entities，把正确名的检索入口堵死。典型例子：**腾讯首席 AI 科学家「姚顺雨」被反复误识为「尧舜禹」**（三个古代圣王，发音完全相同，AI 不会怀疑）。

`--hotwords` 只对 `funasr-nano` 生效，默认引擎 whisper **完全不吃热词**，所以必须用**事后校验 + 强约束**兜底，三件缺一不可：

1. **术语表** `references/asr_glossary.txt` —— 机器可读的对照表，格式 `错误写法 | 正确写法 | 语境限定`。新增词条前先想：这个词会不会在正常语境下合法出现？会 → 必须写语境限定（历史类内容里出现"尧舜禹"是本义，不能动）。
2. **派发子代理 prompt**（详见下方「批量队列」章节）—— 把术语表里**本批相关**的条目原样抄进 prompt，不要凭记忆复述（参考「十四节模板必须抄进 prompt」那条教训，否则 AI 会再次遗漏）。
3. **校验脚本** `scripts/check_glossary.py` —— 每次跑完生成必须扫一遍 `raw/`：
   ```
   python scripts/check_glossary.py --dir <RAW_DIR>        # 只报告
   python scripts/check_glossary.py --dir <目录> --fix                  # 自动替换 ALL 语境
   python scripts/check_glossary.py --dir <目录> --fix --force          # 全替换（含需判语境的）
   ```
   脚本会跳过「误识对照表」行（同行同时出现错误+正确写法视为订正记录，不是污染）；frontmatter 的 `entities` 同样会扫——这是检索入口，必须清。

**事故记忆**：2026-09-06 一次性揪出 19 处「尧舜禹」污染，两个文件全篇错误写法连 frontmatter `entities` 都进了，导致搜"姚顺雨"永远搜不到那条。**写作时遇到任何疑似专名，必须先查表再下笔**——这就是把「怀疑」交给机器、把「确信」留给人。

## 上下文节流规范（2026-09-06 起）

跑大批量任务时，**主线程的上下文是稀缺资源**——一旦被压缩，前面的细节只剩摘要，子代理回报、规则条款全丢。规范：

| 陷阱 | 反例 | 正例 |
|---|---|---|
| **台账查询大 JSON 直接打 stdout** | `... 2>&1 \| tail -20` 把 33KB JSON 灌进上下文 | `... > file.json` 重定向，Python 只打汇总行：`print('总行数',len(rows))` |
| **IMA 批量上传在主线程做** | 主线程跑 create_media ×10，每次 ~1.5KB 凭证（~15KB 净增） | 派 2 个子代理各负责 5 个，凭证上下文落在它们那边；主线程只收"10 个 media_id 已入库"一句话 |
| **长任务中途硬撑到压缩** | 一轮跑 17 条全程不打断 | 跑到一半主动报进度问"继续还是开新会话"，避免压缩后丢上下文 |
| **素材包读进主线程** | 直接 Read 几万字素材包 | 全部甩给子代理，主线程只接文件路径 + 大小回报 |

**默认行为**：`bili_asr.py` 素材包写盘后主线程就不再读取，所有 14 节生成都在子代理侧完成——这条已经做对了，要守住。**新增条目**：跑 `verify_structure.py` 后用 `tail -5` 看汇总，不要 `-A` 看全文。

## 断点恢复：对话中断后如何核实真实进度

长任务（30+ 条）常因对话中断、token 过期或上下文丢失而断在半路。**中断后不要凭记忆回答，也不要采信 subagent 的进度汇报**——实测有 subagent 谎报「16/16 完成」，查盘后实际一条都没回填。

三步交叉核实，三处一致才算真完成：

| 步骤 | 查什么 | 说明 |
|---|---|---|
| 1 | `.workbuddy/cache/bili/index.json` | 条目数；目标 BV 是否入库 |
| 2 | `raw/` 目录按 mtime 排序 | 纪要实际落盘时间与数量，是本轮最硬的证据 |
| 3 | 台账三列交叉 | `状态`=已完成 + `纪要`列非空 + `IMA转存` 状态，三者同时对得上 |

判读规则：
- index.json 有 BV、raw 有纪要、台账状态=已完成 → 该条闭环，只差 IMA
- index.json 有 BV 但 raw 无纪要 → 转写完成、纪要待生成
- 判读规则里不再用「transcript 为空」判断是否跑完收尾——2026-09-02 起素材包是归档保留的，`transcript` 会指向 `cache/bili_subs/` 下的路径。判断进度改用 `raw/` 的 mtime + 台账状态。
- 台账 `状态`=已转写但 raw 无纪要 → 待生成纪要
- 台账 `纪要`列有值但 raw 文件不存在 → 文件被误删或挪走，需排查

**踩过的坑**
- **Git Bash 的 `/tmp` 对 Windows Python 不可见**（Python 会解析成 `\tmp\` 报 FileNotFoundError）。临时脚本一律写 Windows 路径 `%TEMP%\`，用完删除。
- **token 30 分钟过期**：30+ 条的任务不要指望一个 token 跑满，分批处理，每批重取凭证。
- **subagent 进度汇报一律以落盘文件为准复核**，汇报「完成」不等于台账已回填。

## 登录态配置（Cookie 持久化）

**不用每次传 `--cookie`，写一次文件即永久生效。**

| 项 | 值 |
|---|---|
| 配置文件 | `~/.workbuddy/.bilibili_cookie`（`bili_asr.py` 的 `COOKIE_FILE`） |
| 格式 | 单行 `k=v; k=v; k=v`，如 `SESSDATA=xxx; bili_jct=xxx; DedeUserID=xxx` |
| 必需字段 | **只有 `SESSDATA`** —— 登录判定是 `'SESSDATA=' in COOKIE_HEADER.upper()`，其余可选 |
| 优先级 | `--cookie` 参数 > 该文件 > 纯匿名（自动补 `buvid3`） |

**只能手动复制，别再试自动化（2026-09-01 实测三条路全断）**

| 路径 | 结果 | 原因 |
|---|---|---|
| 读 Chrome Cookie 库 + DPAPI 解密 | ✗ | Chrome 127+ 用 **v20（App-Bound Encryption）**：主密钥能用 DPAPI 解出，但每条 value 须经 Chrome 自身服务再解一次，第三方拿不到。实测 34 条 bilibili cookie 全为 v20 |
| `--remote-debugging-port=9222` + CDP | ✗ | 沙箱网络隔离，`netstat` 查不到 9222 监听 |
| Console 执行 `document.cookie` | ✗ | **SESSDATA 的 `is_httponly=1`**，JS 读不到（同批的 bili_jct / DedeUserID 是 0，所以能读到的恰恰不含 SESSDATA） |

附：Chrome 运行时 Cookie 库被独占锁（`WinError 32`），须先关闭 Chrome 才能复制读取。`scripts/chrome_cookie_export.py` 已实现 v10/v11 解密与 v20 识别，将来 Chrome 若回退加密方式可直接复用。

**手动步骤（只要 SESSDATA 一个值）**
1. Chrome 打开 `www.bilibili.com`，确认已登录（能看到头像）
2. 按 `F12`
3. 点标签栏最右的 `>>` → 选 **Application**（应用）
4. 左侧 **Storage → Cookies** → 展开点 `https://www.bilibili.com`
5. 右侧 **Filter 框输入 `SESSDATA`**（省去滚动翻找）
6. 双击该行 **Value** 列 → 全选变蓝 → `Ctrl+C`

拿到后写进 `~/.workbuddy/.bilibili_cookie`，内容就一行：`SESSDATA=粘贴的内容`。

备选（Application 找不到时）：F12 → **Network** → 刷新页面 → 点第一个请求 → Headers → Request Headers → `cookie:` 整串复制（含 HttpOnly 字段）。

**配了能拿到什么**
- **字幕直取路由开放**：视频开了 CC/AI 字幕时秒级出全文，跳过 Whisper（批量场景从分钟级/条 降到 秒级/条）
- 大会员 / 付费视频可下载音轨
- 高码率音源，ASR 准确率略升

**局限（别抱过高期望）**
- 字幕直取的前提是**该视频 UP 主开放了字幕**，没开的照样走本地 ASR。纪实类 UP 主开字幕比例不高，配了 Cookie 也可能仍走 ASR 路由——这是正常的，不是配置失败。
- `SESSDATA` 有效期约 1 个月；改密码、退出登录会立即失效，届时需重新配。

**安全**：Cookie 等同账号凭据。该文件在用户 home 下，**不要提交 git、不要贴进公开场合**。用户把 Cookie 贴进对话时，用完即弃，不写入 memory 或任何日志。

**验证是否生效**：跑一个已知有字幕的视频，看返回 JSON 的 `route` 字段是否变为 `subtitle:xxx`；素材包若出现「未配置登录凭据」说明没读到。

## 注意事项
- 大会员/付费视频需 Cookie 才能下载音轨。
- 转写是口播内容，含口语重复与 ASR 错字；总结时归纳而非照抄，疑问处标注 `[原文疑似]`。
- 评论仅作舆论参考，不作为观点依据。
- 未配置 Cookie 时字幕路由恒定不可用（B站字幕接口返回空、AI 摘要接口返回 `-101`），直接走本地 ASR，不必反复尝试。
- 报 `cublas64_12.dll is not found`：CUDA DLL 未注册，检查 venv 中 `nvidia-*` 包是否完好。
- 报 `No such file or directory ... .m4a`：yt-dlp 产物路径解析失败，脚本已内置 stem 兜底，仍报错则检查 `--keep-audio` 与磁盘权限。
- pip 装包必须传 `PYTHONPATH=`，用腾讯云镜像 `https://mirrors.cloud.tencent.com/pypi/simple`（实测 39MB/s）；阿里云镜像下大包会超时失败。
