---
name: bilibili-video-summary
description: 用户发送 B站（bilibili）视频链接、BV号、av号或 b23.tv 短链，要求总结视频观点/要点/内容，或要求把视频内容整理成知识库条目时使用。通过本地 Whisper 模型下载音频并离线转写（GPU 加速），再基于转写全文生成 14 节知识库条目（含 YAML 元数据、检索入口表、实体表、时间线、待验证清单、术语表）。触发词：B站、bilibili、BV号、b23.tv、这个视频讲了什么、总结视频、视频要点、存知识库、知识库条目、归档。
version: 2.1.0
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
| `--only-meta` | 只抓元信息，判断视频是否值得细看 |
| `--batch-file <json>` | **批处理模式**：`[{"url":..., "out":...}]`，整批只加载一次模型 |

**单条**与**批处理**共用同一套逻辑，区别只在模型加载次数。批量任务一律走批处理。

### 3. 路由逻辑（自动）
1. **字幕直取**（秒级）：需登录态 + 视频开放 CC/AI 字幕 → 直接用
2. **本地 ASR**（分钟级）：无字幕或匿名模式 → 下载音轨 + GPU 转写
3. 返回 JSON 的 `route` 字段标明实际走了哪条路

### 4. 阅读素材包
用 Read 读取 `out` 路径的 Markdown：基本信息、简介、分P、章节、带时间戳正文、热评 Top20。
**必须基于正文全文总结，不得只依赖标题/简介/评论。**

### 5. 生成知识库条目

产物**只有一种格式**：`references/knowledge-entry-template.md` 定义的 14 节知识库条目。

写入 `raw/<发布日>_<完整标题>_<UP主>_纪要.md`。

不区分"简单总结"和"存知识库"——用户存这些文件就是为了能搜到，精简版省下的那点篇幅，代价是半年后检索不到。用户明确要求极简摘要（如"三句话说完"）时，直接在回复里答，不落盘。

**命名规则（硬规则）**：`<发布日>_<完整标题>_<UP主>_纪要.md`
- 发布日取视频发布日，不是处理日
- 标题原文照抄，只剔除 Windows 非法字符 `\ / : * ? " < > |`；全角标点（，？【】）**保留**
- 队列模式下直接用 `--note-name <BV号>` 拿标准文件名，别自己拼
- 正确示例：`2026-01-05_【伯爵】借债，卖官，罚款，古代统治者有哪些增收手段？一期讲清古代非税收入_河畔的伯爵_纪要.md`

### 6. 内容要点写法

见 `references/knowledge-entry-template.md` 第四节：**能用表格就不用散文**。表格每格都是可检索单元，散文里的句子搜不到。

---

## 输出结构（14 节）

完整结构见 `references/knowledge-entry-template.md`，必须按序写全：

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

**tags / entities 决定半年后能不能搜到**，填写粒度见模板。写完全文再回头补这两项——只有通读完才知道这条真正讲的是什么。

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
| `--meta-only` | 只查重 + 补元信息，不转写（几秒一批） |
| `--limit N` | 本次最多处理 N 条 |
| `--raw-dir` | **纪要**落盘目录，默认 `<RAW_DIR>` |
| `--cache-dir` | **素材包**缓存目录，默认 `<CACHE_DIR>` |
| `--note-name <BV号>` | 只输出该 BV 的标准纪要文件名，不碰台账 |
| `--finish <BV号>` | 收尾模式：回填纪要 + 状态置「已完成」+ **删除缓存里的素材包** |
| `--summary <路径>` | 配合 `--finish`，写入「纪要」列 |
| `--ima <状态>` | 配合 `--finish`，写入「IMA转存」列 |

输出 JSON 含 `duplicates` / `processed` / `failed` 三组；`processed[].note_name` 直接给出标准纪要文件名。

标准节奏：先 `--meta-only` 跑一遍（用户确认视频对不对）→ 再不带参数跑转写（素材包落缓存，stderr 打出建议纪要名）→ AI 读素材包写纪要到 `raw/`、传 IMA → `--finish` 回填并清掉素材包。

```bash
# 收尾（纪要用标准名；路径一律正斜杠，Git Bash 会把 \ 转成 // 污染台账字段）
... library_queue.py --token <op_token> --finish BVxxxxxxxxx \
    --summary '<RAW_DIR>/2026-01-05_完整标题_UP主_纪要.md' \
    --ima 已转存
# 输出里 removed 列出被删的素材包，台账「素材包」列自动写成「已清理(日期)」
```

**素材包不进知识库**：转写全文是中间产物，落 `.workbuddy/cache/bili/`（Obsidian 不索引），`--finish` 后删除。可追溯性由纪要的「信息完整性」章节承担——里面记着时间戳覆盖范围与 ASR 误识对照表。真需要回查全文就重跑转写，约 1 分钟。

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

## 产物去向

**只留纪要，不留素材包。**

| 产物 | 去向 | 生命周期 |
|---|---|---|
| 素材包（转写全文 `bili_<BV>.md`） | `.workbuddy/cache/bili/` | 中间产物，`--finish` 收尾时删除 |
| 纪要 / 知识条目 | `<RAW_DIR>` | 长期保留，命名见第 5 步硬规则 |
| 纪要副本 | IMA「B站视频转录」kb_id `<YOUR_IMA_KB_ID>` | 上传一次即归档 |

IMA 通道不可用时**降级**：只落本地，把 IMA转存 保持「未转存」并在回复中说明「IMA 未连接，本次仅本地归档」，核心流程不中断。上传成功后把该行 IMA转存 改为「已转存」；失败改「失败」并在备注写原因。**只有看到「已转存」才算归档完成**，不要凭推测标注。

### IMA 上传三步走

**只传纪要，素材包不传**——素材包是中间产物，传上去会污染检索结果，且 IMA 没有删除接口，传错了只能手动清理。

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

**台账收尾（批量）**：22 条级队列不要逐条 `--finish`，写一次性 Python 循环：读台账 → 对每条非「已完成」行回填（状态=已完成、纪要=标准路径、IMA转存=已转存、素材包=已清理(日期)）→ 删除缓存素材包 → 清 index.json 的 transcript 字段。复用 `library_queue.py` 的 `update()` 即可。

踩过的坑：
- `bucket_name` 常**已带 appid 后缀**，再拼一次 `-{appid}` 会 404 `NoSuchBucket`。脚本内置多 host 候选（`bucket.cos.region` → `bucket-appid.cos.region` → `custom_domain`），逐个试并把每次失败打到 stderr。
- `custom_domain`（`*.image.myqcloud.com`）是图片 CDN，PUT 必 403 且可能不解析，排最后。
- 报 `InvalidAccessKeyId` = 凭证抄错。**不要手抄长 token**：把 `create_media` 整个返回原样写进临时文件再喂脚本，抄错一位就 403；用完立即删（含密钥）。
- 只传 `cos_credential` 子节点会丢 `media_id`，脚本先取顶层 `media_id` 再下钻。
- 上传的 `file_name` 用与本地一致的标准名，IMA 里才能对得上。
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
| 一 | 逐条查重 + 补元信息 | 0（不加载） |
| 二 | `--batch-file` 一次性转写全部 | **1** |

省下的是 (N-1) × 4s 的模型加载 + 每次的进程启动开销。10 条批量约省 40–50s。

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

## 精度实测结论
中文口语场景下，**turbo 与 large-v3 的准确率差距远小于 3 倍的速度差**：
- large-v3 正确、turbo 错：生态位、姓魏的商人、小浣熊、沈殿霞
- **两者共同短板**：人名、品牌名、金额数字（"安藤百福"→"安等/安登百福"；"一角五分"→"一脚五分/195分"）
- 结论：**默认 turbo**。术语密集或需逐字引用时才用 large-v3；数字与专名在产出中一律标注 `[原文疑似]`，并在「信息完整性」列出误识对照表。

## 注意事项
- 大会员/付费视频需 Cookie 才能下载音轨。
- 转写是口播内容，含口语重复与 ASR 错字；总结时归纳而非照抄，疑问处标注 `[原文疑似]`。
- 评论仅作舆论参考，不作为观点依据。
- 未配置 Cookie 时字幕路由恒定不可用（B站字幕接口返回空、AI 摘要接口返回 `-101`），直接走本地 ASR，不必反复尝试。
- 报 `cublas64_12.dll is not found`：CUDA DLL 未注册，检查 venv 中 `nvidia-*` 包是否完好。
- 报 `No such file or directory ... .m4a`：yt-dlp 产物路径解析失败，脚本已内置 stem 兜底，仍报错则检查 `--keep-audio` 与磁盘权限。
- pip 装包必须传 `PYTHONPATH=`，用腾讯云镜像 `https://mirrors.cloud.tencent.com/pypi/simple`（实测 39MB/s）；阿里云镜像下大包会超时失败。
