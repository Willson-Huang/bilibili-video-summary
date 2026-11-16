# 批量队列与台账

批量收集链接时用这套。两条入口：在线表台账（有人值守、能看进度）与本地 CSV（离线或一次跑完）。

## 一、在线表台账

| 项 | 值 |
|---|---|
| 表名 | B站视频转录台账 |
| database_id | `<YOUR_DATABASE_ID>`（环境变量 `BILI_DATABASE_ID` 注入） |
| 位置 | 我的文档 › BILIBILI视频内容音频转录 |
| 访问 | 平台「我的文档」页内打开，直链含私有 ID，不入库 |

字段 12 列：视频链接 / BV号 / UP主 / 视频标题 / 时长 / 状态 / 转写耗时(秒) / 素材包 / 纪要 / 处理时间 / IMA转存 / 备注。

两个状态列职责不同，不要混用：

| 列 | 含义 | 取值 |
|---|---|---|
| 状态 | 本地转写进度 | 待处理 / 已转写 / 已完成 / 失败 |
| IMA转存 | 上传 IMA 知识库的进度 | 未转存 / 已转存 / 失败 / 不适用 |

写入 select 传选项文本即可。「不适用」用于无需上传的场景（用户明确只要本地存档）。新记录默认「未转存」，上传成功后改「已转存」。状态列实测没有「重复」选项，所以重复项不写台账。

## 二、查重（必须先做）

新链接进入处理前，把它的 BV号 与表中「已转写 / 已完成」行的 BV号 比对。**命中即直接终止**：不转写、不生成纪要、不归档、不写台账（不补行、不改状态）。台账里本来没有这条也没关系，向用户说明「与哪条重复」即可，不要造一条「重复」记录。

不走台账时的兜底查重（单条直跑 `bili_asr.py` 时手里没有台账可比）：

1. 扫 `raw/*.md` 看是否已有同 BV 号的纪要
2. 扫 `.workbuddy/cache/bili_subs/*BV*` 看素材包是否已归档

两者任一命中即视为已处理，直接终止本次处理（不转写、不写台账），台账缺这条行属正常。素材包归档是常设机制，旧视频的素材包通常还在。教训：一次验证直接跑了 `bili_asr.py`，漏了这步，把一部早已归档的 39 分钟视频又转了一遍，白付一次网络与算力成本。

## 三、library_queue.py

```bash
PYTHONPATH= "<python>" "<skill>/scripts/library_queue.py" \
  --token <op_token> [--meta-only] [--limit N] [--model <模型>]
```

| 参数 | 作用 |
|---|---|
| `--token` | 资料库凭证，由 `connect_open_platform` 取得，**有效期 30 分钟**，过期重取。同样会留在 shell 历史，批量脚本里从环境变量读，不要写死在命令行 |
| `--meta-only` | 只查重 + 补元信息，不转写（几秒一批） |
| `--limit N` | 本次最多处理 N 条。**只限制本轮处理量，不代表台账只有 N 条待处理**——被截断的条数会在 `report.skipped_by_limit` 与 stderr 告警里报出，必须再跑一轮补完（实测漏看截断提示导致台账两条只办一条）。不确定时不要传，默认一次办完 |
| `--up <UP主>` | 只处理指定 UP主（按「UP主」列精确匹配），适合按系列分批 |
| `--engine whisper\|funasr-nano` | 透传给 `bili_asr.py`；不传则队列跑 whisper，专名密集批次要显式指定 |
| `--hotwords <文件>` | 透传给 `bili_asr.py` 的热词文件路径（仅 Nano 生效） |
| `--force-asr` | 透传给 `bili_asr.py`，强制跳过字幕直取走本地 ASR。专名密集内容即使有 AI 字幕也应带上 |
| `--raw-dir` | 纪要落盘目录（脚本内置默认指向书房的 `raw`） |
| `--cache-dir` | 素材包缓存目录（脚本内置默认指向书房的缓存目录） |
| `--note-name <BV号>` | 只输出该 BV 的标准纪要文件名，不改台账；但仍需 `--token`（argparse 强制）。索引缺行或字段不全时会从素材包自动反解补齐 |
| `--finish <BV号>` | 收尾模式：回填纪要，且归档素材包到 `bili_subs` 成功后才置状态「已完成」；归档失败返回 error、不动台账。归档前自动跑素材包校验，不过则拒绝收尾；索引缺行也会从素材包补齐 |
| `--summary <路径>` | 配合 `--finish`，写入「纪要」列 |
| `--ima <状态>` | 配合 `--finish`，写入「IMA转存」列 |

**token 过期的症状不是鉴权报错**，别误判数据丢了：

- `--finish` 报 `{"error": "未找到 BVxxxxxxxxx"}`：BV 明明在台账里，其实是 token 过期导致查询返回空
- `query_database_record.py` 报 `code=NETWORK_ERROR` 或「台账总行数 0」

遇到这两种，先重取 token 再重试，不要去改台账，不要以为记录被删了。

**两个脚本的 token 传参方式不同**：`library_queue.py` 用 `--token xxx`；`query_database_record.py` / `batch_update_database_records.py` / `batch_delete_database_records.py` 必须用 stdin 管道传——`echo "<token>" | ... --token-stdin`，直接 `--token xxx` 会返回 `code=AUTH_REQUIRED; msg=token 缺失或无效`，且 `--help` 也同样报这个错（不是脚本坏了）。另外 `query_database_record.py` 没有 `--out` 参数，导出用 shell 重定向 `> file.json`。

输出 JSON 含 `duplicates` / `processed` / `failed` 三组；`processed[].note_name` 直接给出标准纪要文件名。

**标准节奏**：先 `--meta-only` 跑一遍（用户确认视频对不对）→ 再不带参数跑转写（素材包落缓存，stderr 打出建议纪要名）→ 读素材包写纪要到 `raw/`、传 IMA → `--finish` 回填并把素材包归档到 `bili_subs/`。

## 四、绕开队列直跑的两处盲区

**盲区一：不写 `index.json`**。为绕开 token 30 分钟过期，大批量转写常改成 `bili_asr.py --batch-file` 直跑。这不是队列路径，索引不更新，随后 `--note-name` 报「缓存索引里没有 BVxxx」，`--finish` 也可能对不上行。开跑前记下 BV 清单，转写完成后补索引：

```python
idx[bv] = {'pubdate': 'YYYY-MM-DD', 'title': '<素材包「- 标题：」原文>',
           'up': '<素材包「- UP主：」去掉括号部分>', 'transcript': ''}
```

四个字段都能从素材包头部「基本信息」直接取。`transcript` 留空比填错安全——归档逻辑会依次尝试 `index[bv]['transcript']` 和默认路径 `cache/bili/bili_<BV>.md`，留空时正好落到实际文件上。

`--note-name` 与 `--finish` 会从素材包自动反解补齐索引（标题 / UP主 / 发布日），手工写法只在素材包本身缺失时才用。自动补齐只在缺字段时触发，不覆盖已有值；素材包找不到时明确报错，不会编造字段。

**盲区二：四列台账字段永远为空**。

- `BV号 / UP主 / 视频标题 / 时长` 四列只在队列的「补元信息」阶段写入；`--finish` 的回填范围仅 `状态 / 处理时间 / 素材包 / 纪要`。
- 所以手工分步处理（双引擎源对照、`--batch-file` 直跑等队列不支持的流程）收尾后，这四列会一直是空，而状态却显示「已完成」。症状是某行只有链接加状态、视频信息全空。
- 补法：按 `record_id` 直改（见下节），值从素材包头部「基本信息」取。它们与本地 `index.json` 一致，但 `--finish` 的自动补齐只写索引、不回写台账。
- `转写耗时(秒)` 素材包不记录，不能从素材包反解。但可以从转写日志反解：批量转写时若把脚本 stdout 落盘（如 `.workbuddy/archive/batch_<日期>/logs/asr*.log`），日志顶层是单条 JSON（形如 `{"ok":true,"batch":true,"count":N,"results":[...]}`），每条含 `asr.asr_sec`。取 `results[].asr.asr_sec` 按 `bvid` 建映射回填。不要按逐行 JSON 解析（会全失配），直接 `json.loads` 取 `results` 数组。判据是先查当时有没有留日志，有则规范化回填（本批 24 条即以 6025.83 秒总量回填并逐条复验通过），日志已流失的老空行才作罢。

## 五、单行字段补改

`--finish` 只能作用于未完成行，对状态已是「已完成」的行会报「未找到 <BV号>」。此时按 `record_id` 直改，不要重跑转写：

```bash
echo "<token>" | python3 ".../library/database/batch_update_database_records.py" --token-stdin \
  --database-id "$BILI_DATABASE_ID" \
  --records '[{"record_id":"<rid>","properties":{"IMA转存":{"select":"已转存"}}}]'
```

**字段值必须写成带类型键的对象**，不能写裸标量：

- 正确：`{"IMA转存": {"select": "已转存"}}`
- 错误：`{"IMA转存": "已转存"}`，会被 `database/_db_types.py` 的 `validate_properties` 整条拒绝，报 `{"error": "records[0].properties 校验后为空，无有效字段"}`
- 类型键取字段自身的类型，从 `get_database_schema.py --database-id <id>` 的 `properties[].type` 取
- 报错时退出码仍是 0（插件老毛病），只判退出码会把失败当成功。判据是看返回体有没有 `error` 键；成功形态是 `{"results": [{"index": 0, "id": "...", "success": true}]}`
- 支持一次 1–100 条。多条改同一列时先发 1 条验证包跑通，确认落库后再发剩余
- 可用 `--stdin` 传入完整请求体 `{"database_id": "...", "records": [...]}`，比 `--records` 拼接长串更稳

```bash
# 收尾（纪要用标准名；路径一律正斜杠，Git Bash 会把 \ 转成 // 污染台账字段）
... library_queue.py --token <op_token> --finish BVxxxxxxxxx \
    --summary '<raw目录>/2026-01-05_完整标题_UP主_纪要.md' \
    --ima 已转存
# 输出里 archived 列出归档后的素材包路径，台账「素材包」列自动更新为归档路径。
# 素材包不在缓存也不在归档目录（或移动失败）时返回 error，台账保持原状态。
```

## 六、本地 CSV 队列

`process_queue.py` 维护本地 CSV 台账，适合一次性批量处理。默认路径指向书房的「BILIBILI视频内容音频转录/处理台账.csv」，可用 `--csv` 覆盖：

```bash
PYTHONPATH= "<python>" "<skill>/scripts/process_queue.py" <参数>
```

| 参数 | 作用 |
|---|---|
| `--init` | 初始化台账（含示例行） |
| `--status` | 查看队列状态（只读） |
| `--meta-only` | 只补元信息，不转写（每条 1–2 秒） |
| 无参数 | 补元信息 + 转写 |
| `--limit N` | 本次最多处理 N 条，同表的截断提醒规则 |
| `--bvid BVxxx` | 只处理指定视频 |
| `--model <模型>` | 指定转写模型 |
| `--csv <路径>` | 指定其他台账 |

| 列 | 填充方 |
|---|---|
| 视频链接 | 用户粘贴 |
| 序号、BV号、UP主、视频标题、时长 | 脚本自动 |
| 状态、转写耗时(秒)、素材包、处理时间 | 脚本自动 |
| 纪要 | 转写完成后由 AI 生成并回填 |
| 备注 | 用户 |

状态流转：空 / 待处理 → 已转写 → 已完成；异常记为 `失败:<原因>`，失败行不会被重复处理。

产物目录（台账同级）：`transcripts/` 放素材包，`notes/` 放纪要。CSV 为 UTF-8 BOM 编码，Excel 直接打开不乱码。

完整流程：粘链接 → `--meta-only` 预览（确认视频对不对）→ 转写 → 读素材包写纪要，回填「纪要」列并把状态改为已完成。

## 七、分P 视频

多P 视频（系列剧、攻略分节）每个 P 是一次独立转写，产出独立素材包 `bili_<BV>_p<页>.md`。

**纪要粒度由内容决定**：判断标准是该 P 能否独立成篇。能独立成篇就每个 P 一份，否则合并成一份并在正文注明覆盖的页号范围。

**进度里是一支视频一行**：多 P 视频只登记裸 BV 号，各 P 的事件都归到这一行；素材包与纪要仍是一页一份。`task_of()` 与 `_norm_id()` 把「BV 号＋`_p<页>`」的写法一律归并成裸 BV 号——上报侧把音轨名整串（`BV1xxx_p1.m4a`）当任务名传进来时，不归并就会给同一支视频单开一行（无标题，显示成裸编号），顶部任务数虚增、总进度被摊薄。

**分P 的时长取全片总长**：`V['duration']` 是整支视频的总长，一行代表整支视频，预计剩余按它算才与实际相符；若改用某一 P 的时长，整支视频的剩余时间会被算成一个零头。

**素材包与纪要按页配套**：`notes_done.py` 会把该 BV 名下的各页素材包一并找齐，每一页都配上纪要才补发完成，只配上一页时跳过并说明还差哪一页；注册「待编译」按纪要文件名去重，各页共用一份纪要时只登记一条。

**归档按全部真实素材包处理**：`--finish` 若只认单个 `bili_<BV>.md`，分P 的 `bili_<BV>_p1..p5.md` 一份都搬不动，会报「素材包归档失败 / 素材包不存在」并拒绝置「已完成」。现在按 `pack_candidates()` 列出全部素材包，逐个搬进 `bili_subs/`，每一份都过 `verify_pack.check_pack`（只查第一份等于放过后四份），缺任意一份即整体失败。台账「素材包」列写第一份路径，其余追加在 `（分P 其余：…）` 里。`verify_pack.py` 另可按素材包头部声明的「分P：N/M」核对页号是否齐全，缺尾页（P5 丢失只剩 P1–P4）也能查出——只按文件名清点时，目录里有的那几份看起来总是齐的。
