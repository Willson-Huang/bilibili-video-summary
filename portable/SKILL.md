---
name: bilibili-video-summary
description: 用户发送 B站（bilibili）视频链接、BV号、av号或 b23.tv 短链，要求总结视频观点/要点/内容，或要求把视频内容整理成知识库条目时使用。通过本地 Whisper 模型下载音频并离线转写（GPU 加速），再基于转写全文生成 14 节知识库条目（含 YAML 元数据、检索入口表、实体表、时间线、待验证清单、术语表）。本版本为「便携版」——不依赖 WorkBuddy 与 IMA，纯标准 Python/Node 脚本 + Markdown 指令，可直接装到 Claude / Cursor / ChatGPT 自定义 GPT 等任意支持自定义指令的 AI 平台。触发词：B站、bilibili、BV号、b23.tv、这个视频讲了什么、总结视频、视频要点、存知识库、知识库条目、归档。
version: 2.1.0-portable
agent_created: true
---

# B站视频观点总结（便携版 / 无 WorkBuddy · 无 IMA 依赖）

## 核心能力
发链接 → 自动转写 → 生成知识库条目。本地 GPU 离线批处理，**不需播放、不需等待实时时长**。

产出**只有一种格式**：14 节知识库条目（含 YAML 元数据、检索入口表、实体表、时间线、待验证清单、术语表）。用户存这些文件是为了半年后还能搜到，所以结构固定，不做精简版。

> 本版本与原 WorkBuddy 版功能等价，但：
> - 去掉了 WorkBuddy「资料库在线表」队列（`library_queue.py`）与 IMA 知识库上传（`create_media`/`add_knowledge`）。
> - 所有路径改为**环境变量 / 相对路径**，不再写死 `C:/Users/<用户名>/...`。
> - 批处理走**本地 CSV 队列**（`process_queue.py`），纪要默认落本地目录，可接你自己的知识库。

## 依赖安装（Requirements）

```bash
# Python 3.10+ 环境
pip install -r requirements.txt        # faster-whisper / yt-dlp / imageio-ffmpeg
# 可选：GPU 加速（NVIDIA），不装则自动回退 CPU
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12   # 或按 CUDA 版本装对应 nvidia-* 包
```

Node（≥ 18）仅用于可选工具（`set-cookie.mjs` 存登录凭据、`bili.mjs` 轻量替代），不装也能跑核心 Python 流程。

自测（不需要网络，装完依赖即可跑）：

```bash
python tests/test_core.py
```

## 一键入口

```bash
cd <本 skill 目录>
python scripts/bili_asr.py "<B站链接>"
# 或（Windows，自动探测 python，env 已封装）
scripts\bili.bat "<B站链接>"
# 或（Linux/macOS，依次探测 python3 / python）
bash scripts/bili.sh "<B站链接>"
```

## 运行环境 / 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `BILI_PYTHON` | `python`（PATH 中） | 运行脚本用的 Python 解释器（装了上述依赖的那个） |
| `BILI_CACHE` | `~/.cache/bilibili-video-summary` | Whisper 模型、临时音频、Cookie 的缓存根目录 |
| `BILI_COOKIE` | `$BILI_CACHE/.bilibili_cookie` | B站登录 Cookie 文件路径（会员/付费视频需要） |
| `BILI_CSV` | `./bilibili_queue.csv` | 本地 CSV 队列路径 |
| `BILI_TMP` | `$BILI_CACHE/tmp/bili_audio` | 音频临时目录 |
| `PYTHONPATH` | 空 | 部分 AI 平台会注入 shim 拦截文件删除 → HF 下载/pip 装大包失败，必须清空 |
| `HF_HUB_DISABLE_SYMLINKS` | `1` | Windows 符号链接解析失败 → `model.bin is incomplete` |
| `HF_HUB_DISABLE_XET` | `1` | hf-mirror 的 xet 后端返回 401 |
| `HF_ENDPOINT` | `https://hf-mirror.com` | 国内拉模型用镜像（可改 `https://huggingface.co`） |

基目录：本 skill 所在目录（脚本用 `__file__` 自动定位，复制到哪里都能用）。

## 工作流

### 1. 提取链接
支持 `bilibili.com/video/BVxxx`、`b23.tv/xxx`、`av123`、`?p=N` 分P。多个链接 → 逐个执行。

### 2. 转写
```bash
# 单条：先落缓存，别直接写进 raw
python scripts/bili_asr.py "<链接>" --model large-v3-turbo --out "素材包/bili_<BV>.md"
```
| 参数 | 说明 |
|---|---|
| `--model` | `large-v3-turbo`（默认，速度与精度平衡）/ `large-v3`（最准）/ `medium`（最快） |
| `--p N` | 指定分P |
| `--force-asr` | 跳过 B站字幕直取，强制本地转写 |
| `--cookie "..."` | 会员视频 / 高码率音源 / 触发字幕路由时使用（也可走 `set-cookie.mjs` 存文件） |
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

写入 `raw/<发布日>_<完整标题>_<UP主>_纪要.md`（目录可自定义，如你的 Obsidian `raw/` 或任意笔记库）。

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

## 批量队列处理（本地 CSV）

`scripts/process_queue.py` 维护本地 CSV 台账，适合一次性批量处理。默认路径 `bilibili_queue.csv`（可用 `--csv` 或 `BILI_CSV` 覆盖）：

```bash
python scripts/process_queue.py --init        # 初始化台账（含示例行）
python scripts/process_queue.py --status       # 查看队列状态（只读）
python scripts/process_queue.py --meta-only    # 只补元信息，不转写（每个 1–2 秒）
python scripts/process_queue.py                # 补元信息 + 转写
python scripts/process_queue.py --limit 3      # 本次最多处理 3 条
python scripts/process_queue.py --bvid BVxxx   # 只处理指定视频
python scripts/process_queue.py --model large-v3
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

> 原 WorkBuddy 版的「资料库在线表」队列（`library_queue.py`，依赖 `op_token` 与平台在线表）已移出核心（见 `../workbuddy/` 原版），便携环境无需它。

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
| 素材包（转写全文 `bili_<BV>.md`） | `素材包/`（或 `--out` 指定目录） | 中间产物，可手动删或 `--keep-audio` 之外默认保留供复核 |
| 纪要 / 知识条目 | `raw/`（自定义，或你的笔记库） | 长期保留，命名见第 5 步硬规则 |

> 原 WorkBuddy 版的「IMA 知识库上传」（`ima_cos_upload.py` + `create_media`/`add_knowledge` MCP）已移出核心（见 `../workbuddy/` 原版）。便携版不强制任何云端知识库——你把纪要写进本地 `raw/` 即可，需要同步到其他 KB 时自行接 API。

## 只增不减（硬规则）

**对已有产出做增强时，原内容必须 100% 保留，只追加新章节，不重排框架。**

具体要求：
- 原文件的章节标题、时间戳、表格、金句说明文字，逐项保留，不精简、不合并、不改写措辞
- 新增要素以**独立章节追加**，不插入或替换原有结构
- 原有小节的编号和顺序保持不变
- 增强后文件体积应**大于**原文件（+50% 以上属正常），若变小说明发生了删减

## 覆盖校验

**重写或增强已有条目后必须执行**，用脚本客观比对，不靠肉眼。初次生成不需要。

```bash
python scripts/verify_coverage.py "<原文件>" "<新文件>"
```

校验三项：
1. **时间戳覆盖** —— 原文件所有 `hh:mm:ss` 是否都在新文件中（应为 0 缺失）
2. **章节标题覆盖** —— 原文件所有 `##`/`###` 标题是否都在新文件中
3. **体积变化** —— 新文件字符数应大于原文件

任何一项不过，说明发生了遗失，必须补回后重新校验。

---

## 实测性能（NVIDIA GPU，仅参考）

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

`process_queue.py` 分两阶段执行，**阶段二用 `--batch-file` 把整批转写合并成一次调用**：

| 阶段 | 动作 | 模型加载次数 |
|---|---|---|
| 一 | 逐条查重 + 补元信息 | 0（不加载） |
| 二 | `--batch-file` 一次性转写全部 | **1** |

也可直接调用批处理模式：

```bash
# items: [{"url": "...", "out": "路径.md"}, ...]
python scripts/bili_asr.py --batch-file <items.json> --model large-v3-turbo
```

输出 `{"ok": true, "count": N, "results": [...]}`，单条失败记在 `results[].error`，不中断整批。

### 已验证不可用的提速方案

**`BatchedInferencePipeline` 不要用。** 实测转写快 2.05x，但输出出现幻觉，且错别字明显增多。2 倍提速是拿质量换的，不值。

## 精度实测结论
中文口语场景下，**turbo 与 large-v3 的准确率差距远小于 3 倍的速度差**：
- large-v3 正确、turbo 错：生态位、姓魏的商人、小浣熊、沈殿霞
- **两者共同短板**：人名、品牌名、金额数字（"安藤百福"→"安等/安登百福"；"一角五分"→"一脚五分/195分"）
- 结论：**默认 turbo**。术语密集或需逐字引用时才用 large-v3；数字与专名在产出中一律标注 `[原文疑似]`，并在「信息完整性」列出误识对照表。

## 适配其他 AI 平台

本 skill 是**通用 Markdown + 标准脚本**，可直接装到：
- **Claude Projects / Cursor Rules**：把本 `SKILL.md` 作为项目指令/规则文件，脚本用终端运行。
- **ChatGPT 自定义 GPT**：把 `SKILL.md` 内容贴进 GPT 的 Instructions，并在「Capabilities」开 Code Interpreter（跑 Python 脚本）。
- **任意支持自定义指令的 Agent**：复制 `SKILL.md` 全文作为 system prompt 片段，脚本路径按你的部署调整。

脚本不依赖任何平台私有 API；需要「抓取 B站字幕/转写」时，AI 只需在终端执行 `python scripts/bili_asr.py <链接>` 并把产物读回即可。

## 注意事项
- 大会员/付费视频需 Cookie 才能下载音轨：用 `node scripts/set-cookie.mjs --cookie "SESSDATA=...; ..."` 存到 `BILI_COOKIE`，或转写时传 `--cookie`。
- 转写是口播内容，含口语重复与 ASR 错字；总结时归纳而非照抄，疑问处标注 `[原文疑似]`。
- 评论仅作舆论参考，不作为观点依据。
- 未配置 Cookie 时字幕路由恒定不可用（B站字幕接口返回空、AI 摘要接口返回 `-101`），直接走本地 ASR，不必反复尝试。
- 报 `cublas64_12.dll is not found`：CUDA DLL 未注册，检查 `nvidia-*` 包是否完好。
- 报 `No such file or directory ... .m4a`：yt-dlp 产物路径解析失败，脚本已内置 stem 兜底，仍报错则检查 `--keep-audio` 与磁盘权限。
- pip 装包必须传 `PYTHONPATH=`（清空平台注入的 shim），用国内镜像更快：`pip install -i https://mirrors.cloud.tencent.com/pypi/simple ...`
