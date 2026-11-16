# 运行环境与登录态

## 一、Cookie 配置

不用每次传 `--cookie`，写一次文件即永久生效。

| 项 | 值 |
|---|---|
| 配置文件 | `~/.workbuddy/.bilibili_cookie`（脚本常量 `COOKIE_FILE`，可用环境变量 `BILI_COOKIE` 覆盖） |
| 格式 | 单行 `k=v; k=v; k=v`，如 `SESSDATA=xxx; bili_jct=xxx; DedeUserID=xxx` |
| 必需字段 | 只有 `SESSDATA`——登录判定是 `'SESSDATA=' in COOKIE_HEADER.upper()`，其余可选 |
| 优先级 | `--cookie` 参数 高于 该文件 高于 纯匿名（匿名时自动补 `buvid3`） |

## 二、为什么只能手动复制

三条自动化路径全部实测失败：

| 路径 | 结果 | 原因 |
|---|---|---|
| 读 Chrome Cookie 库加 DPAPI 解密 | 失败 | Chrome 127 以后用 v20（App-Bound Encryption）：主密钥能用 DPAPI 解出，但每条 value 须经 Chrome 自身服务再解一次，第三方拿不到。实测 34 条 bilibili cookie 全为 v20 |
| `--remote-debugging-port=9222` 加 CDP | 失败 | 沙箱网络隔离，`netstat` 查不到 9222 监听 |
| Console 执行 `document.cookie` | 失败 | `SESSDATA` 的 `is_httponly=1`，JS 读不到（同批的 bili_jct / DedeUserID 是 0，能读到的恰恰不含 SESSDATA） |

另外 Chrome 运行时 Cookie 库被独占锁（`WinError 32`），须先关闭 Chrome 才能复制读取。`chrome_cookie_export.py` 已实现 v10/v11 解密与 v20 识别，将来 Chrome 若回退加密方式可直接复用。

## 三、手动步骤（只要 SESSDATA 一个值）

1. Chrome 打开 `www.bilibili.com`，确认已登录（能看到头像）
2. 按 `F12`
3. 点标签栏最右的 `>>`，选 Application（应用）
4. 左侧 Storage → Cookies，展开点 `https://www.bilibili.com`
5. 右侧 Filter 框输入 `SESSDATA`（省去滚动翻找）
6. 双击该行 Value 列，全选变蓝后复制

拿到后写进 `~/.workbuddy/.bilibili_cookie`，内容一行：`SESSDATA=粘贴的内容`。

备选（Application 找不到时）：F12 → Network → 刷新页面 → 点第一个请求 → Headers → Request Headers → `cookie:` 整串复制（含 HttpOnly 字段）。

## 四、配了能拿到什么

- **字幕直取路由开放**：视频开了 CC/AI 字幕时秒级出全文，跳过 Whisper，批量场景从分钟级每条降到秒级每条
- 大会员 / 付费视频可下载音轨
- 高码率音源，ASR 准确率略升

局限：

- 字幕直取的前提是该视频 UP主 开放了字幕，没开的照样走本地 ASR。纪实类 UP主 开字幕比例不高，配了 Cookie 也可能仍走 ASR 路由，这是正常的，不是配置失败
- `SESSDATA` 有效期约 1 个月；改密码、退出登录会立即失效，届时需重新配
- 未配置 Cookie 时字幕路由恒定不可用（字幕接口返回空、AI 摘要接口返回 `-101`），直接走本地 ASR，不必反复尝试

**安全**：Cookie 等同账号凭据。该文件在用户目录下，不要提交 git、不要贴进公开场合。用户把 Cookie 贴进对话时，用完即弃，不写入记忆或任何日志。

**验证是否生效**：跑一个已知有字幕的视频，看返回 JSON 的 `route` 字段是否变为 `subtitle:xxx`；素材包若出现「未配置登录凭据」说明没读到。

## 五、环境类故障

- 报 `cublas64_12.dll is not found`：CUDA DLL 未注册，检查 venv 中 `nvidia-*` 包是否完好
- 报 `No such file or directory ... .m4a`：yt-dlp 产物路径解析失败，脚本已内置 stem 兜底，仍报错则检查 `--keep-audio` 与磁盘权限
- pip 装包必须传 `PYTHONPATH=`，用腾讯云镜像 `https://mirrors.cloud.tencent.com/pypi/simple`（实测 39MB/s）；阿里云镜像下大包会超时失败
- 大会员与付费视频需要 Cookie 才能下载音轨
- 转写是口播内容，含口语重复与 ASR 错字；总结时归纳而非照抄，疑问处标 `[原文疑似]`
- 评论仅作舆论参考，不作为观点依据
