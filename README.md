# 课程任务脚本 v2.5.0（vision-judge）

Windows 下的网课章节页辅助脚本，基于 Python + Playwright。自动处理 **video（视频）**、**reading（阅读）**、**exercise（练习）**、**empty（空白章节）** 四类任务：播放视频、滚动阅读、按答案自动填选/提交、随后自动进入下一节。

答案来源三层兜底：**答案册 → 题库 → AI**。其中 AI 模式默认开启**视觉判题**，截图把题面发给视觉模型识别，绕开部分课程网站的字体反爬（DOM 里取出的题干是乱码，如"峉峌峍峊峏峎峋"），以截图内容为准作答。

> ⚠️ 请仅用于你有权限访问的课程，并遵守课程与平台规则。脚本不能保证答案正确，也不能保证适配所有课程页面；涉及自动提交时请按需人工核对。

## 目录

- [环境要求](#环境要求)
- [安装](#安装)
- [快速开始](#快速开始)
- [作答模式](#作答模式)
- [答案册（仅自动作答模式）](#答案册仅自动作答模式)
- [AI 作答](#ai-作答)
- [题库查询](#题库查询)
- [配置](#配置)
- [命令行参数](#命令行参数)
- [常见暂停原因与处理](#常见暂停原因与处理)
- [联网与代理](#联网与代理)
- [分享与隐私](#分享与隐私)

---

## 环境要求

- **Windows** 10/11
- **Python 3.10 或更高版本**。安装时勾选 *Add Python to PATH*（或 *Python Launcher*），并保留 *tcl/tk and IDLE*（答案录入窗口需要）。脚本不会安装 Python 本体。
- **可访问 AI/题库接口的网络**（尤其使用 AI 判题时，见[联网与代理](#联网与代理)）。

---

## 安装

1. 安装 Python（见[环境要求](#环境要求)）。
2. 将压缩包**完整解压**到一个文件夹。
3. 双击 `install.cmd`，联网等待本文件夹的 `.venv`、依赖与 Chromium 安装完成。
   - 已有环境缺 pip 时自动用 ensurepip 补装；不会删除或改动系统 Python。
   - 安装失败会停止并显示错误。
4. 双击 `start.cmd` 开始使用。

---

## 快速开始

1. 双击 `start.cmd`。
2. 粘贴**具体课程章节页**完整网址（`http://` 或 `https://` 开头）。
3. 在打开的浏览器中登录并进入任务页，回到终端按 Enter 开始。
4. 默认（手动答题模式）行为：
   - **视频 / 阅读 / 空白章节**自动处理。
   - **练习页**：脚本暂停等待，由你在浏览器自行作答并提交，完成后回终端按 Enter，或等脚本检测到"提交成功"后自动进入下一节。
5. 流程正常结束或暂停后，可先在浏览器核对结果，再按 Enter 关闭。

> 默认即手动答题模式，不加载、不选择答案册。需要自动填答请用[作答模式](#作答模式)。

---

## 作答模式

### 手动答题模式（默认）

视频、阅读、空白章节自动处理；练习页由你在浏览器作答并提交。等待期间每秒检测一次提交成功提示，也接受终端 Enter 通知。

- 无成功提示时，第一次按 Enter 提示"未检测到"，再按一次**强制继续**。
- 等待上限由 `manual_exercise_timeout_seconds` 控制（默认 3600 秒）。
- 若你在浏览器里自己点了"下一节"，脚本检测到章节变化后不会重复点击。
- 此模式忽略 `config.example.json` 的 `chapter_answers`；`--auto-submit` 不生效。

### 自动填答 + 自动提交

按答案册自动选择选项并提交，提交后自动进入下一节：

```powershell
.\start.cmd -AutoAnswer
```

只填答案、人工核对后手动提交：

```powershell
.\start.cmd -AutoAnswer -NoAutoSubmit
```

> 自动提交会处理"是否提交"的确认弹窗（浏览器原生 confirm 与页面内弹窗都会自动点确认/确定/是）。**默认（`verify_submit_success: false`）不验证提交成功**：点完提交并点掉确认弹窗后，直接点击"下一节"继续——即使页面的成功文案不是标准关键词也能继续。若需要等确认到"提交成功"提示再走，可在配置设 `verify_submit_success: true`。

限制本次处理数量：

```powershell
.\start.cmd -MaxTasks 1
```

无界面（无人值守）运行：

```powershell
.\start.cmd -Headless -Url "https://...章节页..." -AutoAnswer
```

---

## 答案册（仅自动作答模式）

- 默认答案册是根目录 `chapter_answers.json`，保留原始 11 个章节答案；**录入工具不覆盖它**。
- 其余答案册在 `answer_books` 文件夹，一册对应一个大类课程，用 `edit_answers.cmd` 新建或修改。新册为空，不复制默认答案。
- 每册保存"章节位置 → 该练习的全部答案"；不同课程的位置、题数、答案可不同。
- `-AutoAnswer` 未指定 `-Answers` 时弹出选择菜单：输入 0 或直接 Enter 用默认册。**请确认所选册与课程一致。**
- 选定答案文件后不会从另一册或配置里的 `chapter_answers` 补答案；缺失或格式不合法会提示或暂停。
- 按章节顺序填答前，题号必须完整对应 1 到该节答案题数；缺题、重复题号或分页子集会**暂停**，不填写也不提交。

录入工具每行填写一个练习所在章节，例如：

```text
1.1.2 = ABCBB
2.3.4 = A(BC)D
```

- 第一行：章节 1.1.2 五道题依次选 A、B、C、B、B。
- 第二行：章节 2.3.4 三道题依次选 A、多选 BC、D。括号内是同一道多选题；不要把两道单选题写进同一对括号。
- 章节号应与页面实际识别位置一致，答案顺序必须与题目顺序一致；录全每处练习，不要漏题。
- 支持 1.2、1.1.2 等点分章节号；仅两段的章节号需页面提供明确的当前章节标记，不从正文小数猜测。

直接指定答案文件跳过菜单：

```powershell
.\start.cmd -AutoAnswer -Answers ".\answer_books\其他大类课程.json"
```

无界面运行不指定 `-Answers` 时用默认册，不显示菜单。自定义答案册不会自动识别课程，也不保证答案正确；题目随机排序、题目变化或分页等情况需重新核对。

> 高级：JSON 答案文件也支持按题目 ID、题目文本 SHA1 或题号填写答案；明确题目答案优先于章节顺序答案。图形录入工具只编辑章节映射。

---

## AI 作答

调用 OpenAI 兼容接口，让大模型判定选择题并自动填入。默认指向 4router 网关（`https://4router.net/v1`，模型 `claude-haiku-4-5-20251001`），`base_url` 与 `model` 可换成任意 OpenAI 兼容的接口。

**默认开启视觉判题（`ai.vision: true`）**，模型需支持图像输入。若用纯文本模型，请把 `ai.vision` 设为 `false`。

### 配置 API key

在解压文件夹新建 `.env` 文件（已加入 `.gitignore`），写一行：

```text
AI_API_KEY=你的key
```

也可用系统环境变量 `AI_API_KEY`。key 只从环境变量或 `.env` 读取，**不要写进源码或 JSON**。

### 三种用法

纯 AI 模式（不加载答案册，全部题目由 AI 判定，自动提交并进入下一节）：

```powershell
.\start.cmd -AIAnswer
```

答案册优先、AI 兜底（答案册没有或题号对不上的题由 AI 逐题作答）：

```powershell
.\start.cmd -AutoAnswer -AIAnswer
```

只填入、不自动提交（人工核对后手动提交）：

```powershell
.\start.cmd -AIAnswer -NoAutoSubmit
```

直接用 Python 时对应 `--ai-answer`、`--auto-answer`、`--auto-submit`。

### 视觉判题（字体反爬场景）

部分课程网站（如超星）用自定义字体反爬：DOM 取出的是乱码，肉眼看正常、文本层面必乱。脚本会：

- 把每道题的页面区块**截图**（2 倍清晰度）发给视觉模型，以图片内容为准判题；
- **终端不再打印乱码的 DOM 题干/选项**，改为在结果行显示模型从截图 OCR 出的题干原文（标为 `OCR题干`）、答案、置信度与理由，便于核对；
- 截图失败时自动退回 DOM 文本判题。

### 行为与安全机制

- 每题单独调用，要求模型返回 JSON（`answer` + `confidence` + `reason`，先推理再作答）；解析失败自动重试，并对常见键名变体容错。
- **单模型直判（默认）**：只用 `ai.model`，不投票不复核；答案硬校验（字母存在、单选恰好一个），校验不过**暂停而不盲选**。
- **模型链**（`ensemble:false` 且配了 `fallback_models`）：主模型置信度低于 `confidence_switch_threshold` 时换备用模型逐个重判；任一达标即采用；全部不足取最高。
- **多模型投票（`ensemble:true`）**：前两个模型独立作答，一致且置信度 ≥ `verify_threshold`（默认 85%）即采用；分歧或低置信度由后续模型复核/仲裁凑多数票。低置信度多数票可被高置信度异议（差距 ≥ 0.15）推翻。
- **模型熔断**：同一模型连续 2 次失败后本次运行内跳过；成功一次即复位。超时请求只补试一次。
- 置信度低于 `min_confidence` 的答案被放弃；默认 0 表示不拦截，想保守可设 0.6~0.8。
- 可缓存判定结果（`ai.use_cache`），默认关闭。

> AI 答案是模型推断，不保证正确；首次使用建议先用"填入不提交"模式核对，提交前扫一眼低置信度的题。

---

## 题库查询

除 AI 外，还支持外部网课题库接口（默认 ZERO 题库 `https://api.gomooc.net/api.php`）。答案链为 **答案册 → 题库 → AI**：题库命中人类录入标准答案时优先，未命中或对不上选项时落回 AI。

启用：

1. 打开 <https://api.gomooc.net> 注册，在"个人中心"获取 token。
2. `config.example.json` 设置 `"tiku": { "enabled": true, "token": "你的token" }`。
3. 与 `-AIAnswer` 或 `-AutoAnswer` 组合使用。

行为：

- 自动从题目区块切出题干（去题号与选项）再查询；
- 返回答案映射回页面选项字母（支持字母直返、答案原文匹配、多答案分隔符、标点空格差异容错）；
- 映射不上、单选匹配多个、接口失败时放弃该题交给 AI，不瞎填；
- 终端打印 `题库 第N题 → X（答案原文: ...）` 便于核对。

> 该接口按次数计费/限量，请以官网为准。

---

## 配置

自动加载 `config.example.json`；未列出的用内置默认。可在该文件覆盖任意键适配你的页面。

常用顶层键：

| 键 | 默认 | 说明 |
|---|---|---|
| `video_threshold` | 1.0 | 视频播放完成阈值（0~1），小于 1 可提前判定完成 |
| `video_stall_timeout_seconds` | 120 | 视频长时间未推进判定卡死的时间 |
| `completion_wait_seconds` | 3 | 任务完成后到下一步的等待秒数 |
| `next_click_interval` | 4.5 | 点击"下一节"后的缓冲间隔 |
| `poll_seconds` | 1 | 轮询检测间隔 |
| `page_ready_timeout` | 15 | 页面就绪等待上限 |
| `next_button_timeout` | 20 | 等待"下一节"按钮出现的时间 |
| `submit_timeout_seconds` | 25 | 提交后等待/处理提交结果的时间上限 |
| `verify_submit_success` | false | 提交后是否等待"提交成功"提示再进入下一节；false=点完提交直接进下一节（默认） |
| `task_timeout_seconds` | 1800 | 单个视频/阅读任务超时 |
| `manual_exercise_timeout_seconds` | 3600 | 手动答题等待上限 |
| `exercise_success_pattern` | （内置正则） | 判定"提交成功"的页面文本（子串匹配） |
| `exercise_success_selectors` | （内置列表） | 判定"提交成功"的页面选择器 |
| `exercise_error_pattern` | （内置正则） | 判定"提交失败"的页面文本 |
| `chapter_answers` | {} | 配置内联的章节答案（`-AutoAnswer` 选择答案册时忽略） |
| `ai` | 见下表 | AI 接口配置 |
| `tiku` | 见下表 | 题库接口配置 |

> 适配页面：可新增 `chapter_selectors`、`reading_selectors`、`submit_selectors`、`next_selectors`、`question_selectors` 等选择器列表。

### `ai` 段

| 键 | 默认 | 说明 |
|---|---|---|
| enabled | false | `--auto-answer` 模式下是否启用 AI 兜底（不影响 `-AIAnswer`） |
| base_url | https://4router.net/v1 | 任意 OpenAI 兼容接口地址 |
| model | claude-haiku-4-5-20251001 | 主模型名（视觉判题需支持图像输入） |
| vision | true | 截图发给视觉模型判题；false 用 DOM 文本 |
| fallback_models | （空） | 备用模型链，主模型低置信度时依次换用 |
| confidence_switch_threshold | 0 | 低于此置信度触发换模型；0 表示不换 |
| ensemble | false | 多模型投票模式 |
| ensemble_models | claude-haiku-4-5-20251001、claude-sonnet-5 | 投票阵容：前两个投票，后续复核/仲裁 |
| verify_threshold | 0.85 | 投票一致但低于该值触发复核；差距 ≥0.15 的高置信度异议可推翻多数票 |
| api_key_env | AI_API_KEY | 读取 key 的环境变量名 |
| timeout_seconds | 60 | 单次请求超时 |
| max_retries | 2 | 重试次数（429 限流会自动长退避） |
| request_interval | 6 | 相邻两次调用最小间隔秒数 |
| min_confidence | 0 | 低于该置信度放弃答案；0 为不拦截 |
| temperature | 0 | 采样温度，建议保持 0 |
| use_cache | false | 是否缓存判定结果（题干哈希 → 答案） |
| cache_path | answer_books/ai_cache.json | 缓存文件位置（仅 use_cache 开启时使用） |

### `tiku` 段

| 键 | 默认 | 说明 |
|---|---|---|
| enabled | false | 是否启用题库查询 |
| api_url | https://api.gomooc.net/api.php | 题库接口地址 |
| token | （空） | 题库 token，官网个人中心获取 |
| timeout_seconds | 15 | 单次请求超时 |
| max_retries | 1 | 网络错误重试次数 |
| request_interval | 2 | 相邻两次调用最小间隔秒数 |

---

## 命令行参数

`start.cmd` 转发到 `auto_tasks.py`，参数一一对应（PowerShell 用 `-Xxx`，Python 用 `--xxx`）。

| start.cmd / PowerShell | auto_tasks.py | 说明 |
|---|---|---|
| `-Url "https://..."` | `--url` | 课程章节页 URL（必填） |
| `-AutoAnswer` | `--auto-answer` | 按答案册自动填答 |
| `-AIAnswer` | `--ai-answer` | 用 AI 判定并自动填入 |
| `-NoAutoSubmit` | （省略 `--auto-submit`） | 填入后不自动提交，人工核对 |
| `-MaxTasks N` | `--max-tasks N` | 本次最多处理 N 个任务（默认 100） |
| `-Headless` | `--headless` | 无界面；需 `-Url` 与已有登录会话 |
| `-Config 路径` | `--config` | 配置文件（默认 config.example.json） |
| `-Answers 路径` | `--answers` | 指定答案册文件（要求 `-AutoAnswer`） |

---

## 常见暂停原因与处理

- **网络错误 `WinError 10061 / 超时`（AI 接口）**：连不上 `base_url`。多为**必须走代理而上网、但代理未开启**，详见[联网与代理](#联网与代理)。换模型解决不了连不上的问题；请先确认网络/代理已通。
- **`当前答案册与题目未匹配`**：题号未完整对应或答案缺失，脚本暂停而不盲选；可用 AI/题库兜底（`-AIAnswer`）。
- **`未收到可验证的提交成功提示`**：出现在 `verify_submit_success: true` 且页面成功文案非标准时；建议用默认的 `verify_submit_success: false`（不验证、提交完直接进下一节），或调整 `exercise_success_pattern`。
- **没有下一节 / 流程结束**：当前章节确实没有后续任务，或成功弹窗遮挡"下一节"。脚本会先关闭成功弹窗再判断。
- **无法确认选中状态**：选项结构与内置选择器不匹配，可补充 `question_selectors` 等配置。
- **视频 / 阅读卡住**：视频长时间未推进，或阅读区域未能在时限内滚动到底部。

---

## 联网与代理

使用 **AI 判题**或**题库**时需要访问外网接口。部分网络（尤其直连受限的环境）必须经过**本地代理**才能上外网。

- 脚本用 Python 标准库 `urllib` 发起请求，**默认不读取系统代理**。
- 若你的网络必须走代理，请在运行脚本的终端先设置代理再启动：

  ```powershell
  $env:HTTP_PROXY  = "http://127.0.0.1:7892"
  $env:HTTPS_PROXY = "http://127.0.0.1:7892"
  .\start.cmd -AIAnswer
  ```

  （端口以你的代理软件实际端口为准，如 7890/10809 等。）

- 若 `base_url` 是境外服务且直连不通，务必通过代理访问。
- 排查：先确认代理软件已打开、对应端口在监听（`netstat -ano | findstr 端口`），再用浏览器/curl 验证能打开 `base_url` 后再跑脚本。

---

## 分享与隐私

- 本仓库保留根目录默认答案册 `chapter_answers.json`（11 个章节）；答案未经正确性核验。
- 自己录入的 `answer_books/*.json`、`.course-browser`（登录状态）、`.venv`、日志与 `.env` 已加入 `.gitignore`，**不应上传或分享**。
- 不要把课程账户、密码、访问令牌写入源码或 JSON。
- 尚未指定开源许可证；公开可见不等于已授予任意使用、修改或再分发许可。
