# 产物去向与 IMA 上传

## 一、什么出本机

| 数据 | 去向 | 是否出本机 |
|---|---|---|
| 素材包（含第三方字幕全文与评论） | `.workbuddy/cache/bili_subs/` | 否，仅本地磁盘 |
| 纪要 / 知识条目 | `<raw目录>` | 否，仅本地磁盘 |
| 纪要副本 | IMA 知识库 | **是**，上传即出本机，且 IMA 无删除接口 |
| 队列元信息（BV号 / 标题 / UP主 / 状态） | workbuddy.cn 在线表 | **是**（仅队列模式） |

「全程本地」只在素材包这一层成立；纪要会进 IMA 云端，队列模式还会把元信息写进在线表。对外说明行为时不要一概说「不上云」。

## 二、产物与生命周期

| 产物 | 去向 | 生命周期 |
|---|---|---|
| 素材包（转写全文 `bili_<BV>.md`） | `.workbuddy/cache/bili_subs/` | 归档保留，不删除 |
| 纪要 / 知识条目 | `<raw目录>` | 长期保留，命名见技能正文的命名规则 |
| 纪要副本 | IMA「B站视频转录」知识库，`kb_id` = `<YOUR_KB_ID>` | 上传一次即归档；内容变更时用 REPLACE 覆盖 |

**素材包必须保留**：它是误识修正的唯一依据（拿字幕原文逐处对照，不凭上下文猜测），删掉等于放弃修正能力。体积代价可忽略：单份 20–150 KB，100 份约 10 MB。实测教训：素材包被 `--finish` 直接删除后，32 条只能靠字幕重跑一遍。

> 知识库 ID 属私有资源，仓库里只留占位符。实际取值用 `get_knowledge_base_list` 现查，或看本机连接器状态文件，不要凭记忆写死。

归档由 `--finish` 内置：脚本自动把 `cache/bili/bili_<BV>.md` 挪到 `cache/bili_subs/`，并把 `index.json` 的 `transcript` 指向新路径，台账「素材包」列同步更新为归档路径。素材包已在归档目录内的（历史批次）自动识别为已归档，不重复移动，不需要手工 `shutil.move`。归档失败（素材包缺失 / 移动出错）时不置「已完成」：输出 error 加原因，台账保留原状态，处理后重跑。

归档目录里存在两种前缀：`sub_BV*.md`（早期批次，18 份）与 `bili_BV*.md`（后续批次）。统计数量时只 glob 一种会漏掉另一批。

## 三、降级

IMA 通道不可用时只落本地，把「IMA转存」保持「未转存」，并在回复中说明「IMA 未连接，本次仅本地归档」，核心流程不中断。上传成功后把该行改为「已转存」；失败改「失败」并在备注写原因。只有看到「已转存」才算归档完成，不要凭推测标注。

## 四、禁止中性化改写

子代理或编译员遇到敏感人名、组织名、政治与地缘表述时，容易自作主张改写成中性说法（把「朱镕基」写成「时任国务院总理」、把具体姓名改成「某位校友」）。正确做法是只区分事实与判断、标明立场归属（「此为 UP 主观点」），绝不淡化、替换或删名。ASR 失真就保留原词并在第十二节列对照表。派子代理写纪要时，必须在指令里显式写这条禁令。

## 五、IMA 上传

只传纪要，素材包不传——素材包只在本地归档供误识修正回查；传上去会污染检索结果，且 IMA 没有删除接口，传错了只能手动清理。

`add_knowledge` 只认 `media_id`，字节流要自己推到 COS。完整链路：

```bash
# 1) 取凭证：mcp__ima-mcp__create_media(
#      knowledge_base_id, file_name, file_ext, file_size, content_type)
#    返回 media_id + cos_credential；content_type 必须精确（md → text/markdown）

# 2) 推字节流（--cred 收整个返回 JSON 的文件路径，也收 JSON 字符串）
PYTHONPATH= "<python3>" "<skill>/scripts/ima_cos_upload.py" \
  --cred <凭证JSON文件路径> --file <本地文件>

# 3) 入库：mcp__ima-mcp__add_knowledge(
#      knowledge_base_id, media_id, duplicate_name_strategy=..._REPLACE)
```

批量上传：把多个 `create_media` 的返回按 `{"file","media_id","cos_credential"}` 拼成 JSON 数组文件，一条命令跑完：

```bash
PYTHONPATH= "<python3>" "<skill>/scripts/ima_cos_upload.py" --batch <凭证数组JSON文件>
```

- 数组元素：`{"file": <本地纪要路径>, "media_id": <create_media 返回的 media_id>, "cos_credential": <原样>}`
- 输出逐条 ok/status，退出码非 0 表示有失败项
- 凭证抄写错误会返回 `InvalidAccessKeyId`，重跑 `create_media` 再补

**台账收尾（批量）**：20 条以上不要逐条 `--finish`（每次要重读台账，token 只活 30 分钟）。写一次性循环：读台账 → 对每条非「已完成」行回填（状态、纪要、IMA转存，攒批调 `update()`）→ 归档素材包（挪到 `cache/bili_subs/` 并更新 `index.json` 的 `transcript`），不删除。单条收尾直接 `--finish`。

⚠️ **手写循环会绕过归档前校验**：`library_queue.py --finish` 内置「归档前跑 `verify_pack`，不过则拒绝收尾」，手写 `shutil.move` 不经过这一步。批量收尾的循环里，归档每个素材包之前必须先校验：

```python
import sys; sys.path.insert(0, r'<skill>/scripts')
import verify_pack
errors, warnings, info = verify_pack.check_pack(pack_path)
if errors:  # 不许归档、不许把该行置「已完成」，先修包
    ...
```

或先跑 `python scripts/verify_pack.py --dir <素材包目录> --strict` 把不合格的挑出来。理由：字段残缺的包一旦入库，追溯能力就断了，而这一点在批量场景下最难事后发现。

## 六、COS 与媒体 ID 的坑

- `bucket_name` 常已带 appid 后缀，再拼一次 `-{appid}` 会 404 `NoSuchBucket`。脚本内置多 host 候选（`bucket.cos.region` → `bucket-appid.cos.region` → `custom_domain`），逐个试并把每次失败打到 stderr
- `custom_domain`（`*.image.myqcloud.com`）是图片 CDN，PUT 必 403 且可能不解析，排最后
- 报 `InvalidAccessKeyId` 说明凭证抄错。不要手抄长 token：把 `create_media` 整个返回原样写进临时文件再喂脚本，抄错一位就 403；用完立即删（含密钥）
- 只传 `cos_credential` 子节点会丢 `media_id`，脚本先取顶层 `media_id` 再下钻
- 上传的 `file_name` 用与本地一致的标准名，IMA 里才能对得上
- `create_media` 偶发返回空（`{"media_id":"","cos_credential":null}`），不是参数错误，原样重试第二次必成，别改参数瞎试
- **`media_id` 抄错不报错**：长十六进制串手写进 batch JSON 时多一位或少一位，COS 推流照样 200，直到 `add_knowledge` 才指向错误对象，此时已无法察觉。校验判据用「以知识库 id `<YOUR_KB_ID>` 结尾 + 长度不小于 80」；全长 90 字符（`markdown_` 9 + 32hex + `_` 1 + 32hex + kb_id 16），按 84 校验会把所有合法 id 判为非法
- **批量覆盖用索引配对**，别手工拼 batch JSON：先把待传清单按固定顺序落成 `ima_rows.json`（含 name/size/path），`create_media` 的返回按同一顺序写成 `ima_cred_<n>.json`，再用脚本按索引号配对拼 batch。手工拼时一旦错位，推流照样 200，只能等 `add_knowledge` 才发现，且已无法挽回
- **并行节奏**：`create_media` ×8 → 写 8 个凭证 → `--batch` 推流 → `add_knowledge` ×8；推流命令与下一批的 `create_media` ×8 同一轮发出，流水作业。63 份实测约 21 分钟
- **凭证用完立即删**：`ima_cred_*.json` 含 `secret_key`。63 份清完一次性删，Windows 下一次删 60 个以上可能被 SIGTERM 打断，分 5 批每批不超过 16 个
- **批量节奏**：`create_media` 逐条取凭证攒够 8 条 → 写进一个 JSON 数组跑 `--batch` → `add_knowledge` 逐条入库。凭证有效期 12 小时（`expired_time - start_time = 43200`），攒批不超时
- **完成证据**：`get_knowledge_base_list` 看 `knowledge_total_size` 增量，传 N 条应正好加 N。这是唯一可信的完成证据，不要用「调用没报错」当证据
- IMA 没有删除接口，传之前先确认文件对不对

## 七、连接器可用性判定

工具「已连接」不等于可用：桌面面板显示已连接、connector-status 显示 connected，都不代表工具暴露给 agent。唯一判定标准是 `ToolSearch` 搜 `mcp__ima-mcp__create_media` 能否搜到。搜不到就是没暴露，别反复重试，直接降级本地归档。

**当前会话内启用连接器不会刷新工具索引**：会话启动时 IMA 未启用，索引快照里没有 `mcp__ima-mcp__*`；会话中途在面板启用后，connector-status 会变成 connected、`connector-states.json` 里 `enabled: true` 也确实写入了，但 deferred tools 索引不刷新，`ToolSearch` 依旧搜不到，直接调用报 `not found in the deferred tools index`。改配置文件无济于事，唯一解法是开一个新会话。批量上传前应先确认工具可用，不要假定「用户说连好了就能用」。

开新会话也不保证成功：新会话启动时 IMA 工具仍未进索引，查 `~/.workbuddy/connectors/*/mcp.json` 里 `connector:ima-mcp` 是 `disabled: true`。新会话只在你启用过的那个配置下才加载得到，`disabled` 字段才是真相。

排查顺序：

1. `ToolSearch` 搜 `mcp__ima-mcp__create_media`，搜到才算可用（唯一判定标准）
2. 搜不到，查 `~/.workbuddy/connectors/*/mcp.json` 里 `connector:ima-mcp` 的 `disabled` 字段。有多个 workspace 配置目录，逐个查，`disabled: true` 的那个生效时就用不了
3. 确认是 disabled，去连接器管理页点「信任/启用」，再开新会话

上传受阻时不要干等：把待传清单写进 `.workbuddy/cache/ima_pending_overwrite.json`（文件路径 + 大小 + kb_id + REPLACE 策略 + 步骤），下次会话可用时直接照着跑。

## 八、覆盖上传的验证

- **REPLACE 前先反查云端 title**：同名判定靠云端 `title`，且含 `.md` 扩展名。用 `search_knowledge` 搜关键词确认 title 与本地文件名逐字一致，不一致会新建一条而不是覆盖，IMA 又没删除接口
- `search_knowledge` 单次返回约 117KB（几乎是全库条目列表，不是命中片段），会自动落盘成 txt，不要直接读，用 Python 从落盘文件里抽 `title` 与 `media_id`
- **覆盖验证挑小的那条抽查**：`fetch_media_content` 拉体积小的那份（如 42KB）确认订正词已生效即可，不要拉 125KB 的，内容直接进上下文，代价太大
- **覆盖上传的验证标准与首次上传不同**：首次上传看 `knowledge_total_size` 传 N 条应加 N；覆盖同名条目时条目数不变，需改看单条 size 变化或抽查内容确认已更新
