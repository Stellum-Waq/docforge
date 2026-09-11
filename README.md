# 文枢 DocForge

> 办公文件转换与处理中枢 · 本地优先（Local-First）· Windows 桌面

这套工具把办公里那些"每天重复点一遍"的活收进一个桌面应用，并且**默认不联网**：
照片转 Excel / 提取文字、PDF ↔ Word、PDF 与图片加水印、扫描件转可搜索 PDF、
多步骤流水线批处理，全部可以离线完成；需要更高精度时再按需开启
**DeepSeek 多模态 OCR**（云端引擎带自适应切片与费用统计）。

除了图形界面，还提供一套**命令行**（`run` / `pipeline` / `watch` / `presets`），
供脚本与计划任务调度 —— 用的和界面是同一个任务队列，行为完全一致。

---

## 架构总览

```
┌──────────────────────────────────────────────────────────────┐
│  UI 层    React 19 + TS + Vite 7 + Tailwind v4 + Motion       │  科技风动效
├──────────────────────────────────────────────────────────────┤
│  桌面壳   Electron 44（main / preload / IPC）                  │  窗口·托盘·菜单
│           contextBridge 安全桥 + safeStorage 加密存储密钥      │  右键菜单·文件关联
├──────────────────────────────────────────────────────────────┤
│  内核     Python 3.11 + FastAPI + Uvicorn                     │  文档处理核心
│           仅绑 127.0.0.1 + 随机端口 + 一次性 Bearer Token       │  任务队列·SQLite
├──────────────────────────────────────────────────────────────┤
│  引擎     Rule Registry：可插拔 + 保真度择优 + 优雅降级         │
│   PDF  : PyMuPDF / pypdf / pdf2docx                           │
│   Word : Word COM（最高保真）→ LibreOffice → 内置渲染兜底       │
│   OCR  : DeepSeek Vision（自适应切片）⇄ RapidOCR（离线）        │
└──────────────────────────────────────────────────────────────┘
```

**设计原则**：Electron 只负责界面与系统集成，Python 负责文档处理，
两者通过本机 HTTP + IPC 桥接。这样既能用 Web 技术做出高质感的动效界面，
又能复用 Python 在文档处理上无可替代的生态。

---

## 环境要求

| 组件 | 版本 | 说明 |
|---|---|---|
| Node.js | ≥ 20.19 | 开发环境实测 v24.14 |
| pnpm | ≥ 10 | 实测 11.21 |
| Python | ≥ 3.10 | 实测 3.11 |
| Microsoft Office | 可选 | **装了则 Word→PDF 走 COM，保真度最高**；不装会自动降级 |
| LibreOffice | 可选 | 无 Office 环境下的次优引擎 |

---

## 快速开始

```bash
# 1) 前端依赖（Electron 二进制较大，走国内镜像）
pnpm install

# 2) Python 内核依赖
python -m venv core/.venv
core/.venv/Scripts/python.exe -m pip install -r core/requirements.txt

# 3) 开发模式启动（Electron 会自动拉起 Python 内核）
pnpm dev
```

## 常用命令

```bash
pnpm dev            # 开发模式（HMR + 内核自动重启）
pnpm build          # 构建前端产物
pnpm typecheck      # 类型检查（主进程 + 渲染进程）
pnpm core:test      # 内核单元测试（288 项）

# 打包分发
pnpm icon           # 生成应用图标（多尺寸 .ico / .png）
pnpm package:core   # PyInstaller 打包 Python 内核 → core/dist/docforge-core
pnpm package:win    # 一条龙：图标 + 内核 + 安装包/便携版 → apps/desktop/release

# 验证工具
pnpm smoke                                          # 端到端冒烟测试（起真实内核跑完整流程）
pnpm probe                                          # 列出已注册动作与引擎可用性
core/.venv/Scripts/python.exe tools/test_packaged.py  # 验证打包后的内核可执行文件

# UI 探针（需应用以 DOCFORGE_DEBUG_PORT=9222 启动）
node tools/cdp.mjs text                 # 导出页面可见文本
node tools/cdp.mjs click "图片水印"      # 按文字点击
node tools/cdp.mjs shot out.png         # 截图（只截本应用，不影响其他程序）
node tools/cdp.mjs img preview.png      # 导出页面上的预览图（服务端渲染结果）
```

---

## 命令行（批处理与自动化）

桌面界面解决的是"我偶尔转几个文件"，但办公场景里还有另一类需求：
**"每天把收件箱里的附件批量转一遍"**。这类活应该交给脚本和计划任务。
内核同时提供一套 CLI，用的是**同一个任务队列** —— 并发、进度、失败隔离、
输出重名策略与界面里完全一致。

```bash
# 列出全部动作及其参数
docforge actions

# 批量执行一个动作（目录会递归展开）
docforge run image.watermark ./照片 -o ./输出 --param text=机密 --param opacity=0.35

# 多步骤流水线：上一步的产物直接喂给下一步
docforge pipeline ./扫描件 -o ./输出 \
    --step pdf.searchable:dpi=300 \
    --step pdf.watermark:text=内部资料,opacity=0.3 \
    --step pdf.compress \
    --step pdf.extract_pages:pages=1-2

# 参数预设：把调好的一套参数存下来复用
docforge presets save image.watermark 公章 --param text=公司公章 --param opacity=0.3
docforge run image.watermark ./照片 --preset 公章 --param text=合同专用章   # 命令行覆盖预设

# 监听文件夹：新文件出现即自动处理（轮询实现，网络盘上比事件通知可靠）
docforge watch ./收件箱 --action doc.to_pdf -o ./转换结果 --interval 3

docforge selfcheck      # 检查本机环境与可用能力
docforge serve          # 启动内核服务（Electron 用的就是这条）
```

**退出码**：`0` 全部成功 · `1` 有文件失败 · `2` 参数或环境错误。
可以直接写进计划任务或 CI 脚本里判断成败。

> **打包注意事项**
> * 内核源码改动后**必须重新执行 `pnpm package:core`** —— PyInstaller 在分析阶段
>   抓取源码，改了代码不重打包，装出来的程序跑的仍是旧逻辑（实测踩过：
>   新增的接口在打包产物里直接 500）。
> * 打包完务必跑一次 `tools/test_packaged.py`。PyInstaller 最典型的失败方式是
>   **程序能启动、一动真格就报错**（ONNX 模型没收集、OpenCV 的 DLL 缺失、
>   pywin32 的 COM 模块找不到），源码运行完全正常，只有装出来才暴露。
> * `pnpm-workspace.yaml` 里的 `patchedDependencies` 修的是 electron-builder 的
>   上游缺陷（详见补丁文件内的说明）。删掉它会导致打包直接失败。

> **断言批量任务时注意**：只看 `job.status` 会**假通过** ——
> 当文件因格式不匹配被跳过时，任务状态同样是 `succeeded`
> （0 成功 + 1 跳过）。必须逐个检查文件级状态（见 `tools/test_packaged.py`
> 里的 `job_all_files_succeeded`）。这个坑已经让一个真实的 HEIC 缺陷
> 从测试里溜过去一次。

---

## 已知限制

* **云端 OCR 未经真实 API Key 联调** —— 请求构造与错误处理按官方文档实现并有测试
  覆盖，但没有跑过真实调用。这是当前最主要的验证缺口。
* PDF 加密 / 解密 / 权限设置尚未实现。
* 未安装 Office 且未安装 LibreOffice 时，Excel/PPT 转 PDF 不可用
  （内置渲染不做 xlsx/pptx —— 靠文字提取没有意义，会丢失全部结构）。
* 「监听文件夹」只在 CLI 提供，界面里没有对应的开关 —— 它天然是常驻进程，
  而内核是随应用启停的。要做成界面功能需要把内核改成常驻服务，
  与"本地优先、用完即走"的当前形态冲突，因此先不做。

> **验证方式说明**：
> * 不要用「屏幕截图 + SendKeys 模拟键盘」去验证桌面应用 —— 一旦窗口没抢到前台，
>   按键会打进用户正在用的其他程序，而且抓屏可能抓到别的窗口，让验证结论完全失真
>   （开发过程中确实发生过）。请使用 `tools/cdp.mjs`。
> * 不要依赖视觉模型判断**低对比度水印、细微几何位置**这类问题 —— 实测它漏看过
>   32% 透明度的水印（还曾把桌面应用截图描述成聊天软件）。这类判定请用
>   可量化的程序化手段（渲染前后逐像素求差、`get_text()` 读回矢量文字等）。

---

## 目录结构

```
.
├─ apps/desktop/            # Electron 应用
│  ├─ src/main/             # 主进程：窗口·内核进程管理·IPC
│  │  ├─ index.ts           #   应用入口与 IPC 注册
│  │  ├─ core.ts            #   Python 内核生命周期管理（握手/探活/重启/事件流）
│  │  ├─ sse.ts             #   SSE 客户端（手写解析，支持分片与重连）
│  │  └─ files.ts           #   路径递归展开与文件分类
│  ├─ src/preload/          # contextBridge 安全桥
│  └─ src/renderer/         # React 界面
│     ├─ src/components/    #   设计系统组件（含 ui/ 原语库）
│     ├─ src/features/      #   功能域：dashboard / image-watermark / batch / taskcenter
│     ├─ src/hooks/         #   内核状态订阅
│     ├─ src/store/         #   工作区文件仓库 + 任务状态仓库
│     └─ src/styles/        #   设计令牌与动效
├─ core/                    # Python 内核
│  └─ docforge/
│     ├─ __main__.py        #   进程入口（端口协商 + 握手 + 父进程看门狗 + CLI 分发）
│     ├─ cli.py             #   命令行：actions / run / pipeline / watch / presets / selfcheck
│     ├─ app.py             #   FastAPI 装配
│     ├─ actions/           #   动作注册表与各功能实现
│     │  ├─ base.py         #     动作抽象、输出路径预定、原子写入、聚合动作
│     │  ├─ image_watermark.py
│     │  ├─ image_ocr.py    #     图片提取文字
│     │  ├─ image_table.py  #     图片转 Excel
│     │  ├─ pdf_watermark.py#     PDF 水印（矢量文字）
│     │  ├─ pdf_toolbox.py  #     合并/拆分/旋转/压缩/提取页/转图片/文档信息
│     │  ├─ pdf_searchable.py #   扫描件 → 双层可搜索 PDF（不可见文字层）
│     │  ├─ pipeline.py     #     多步骤流水线（串起任意动作）
│     │  └─ doc_convert.py  #     Office↔PDF 转换（三级引擎路由）
│     ├─ engines/com.py     #   Office COM 自动化（专用线程 + 看门狗 + 进程回收）
│     ├─ pdf/geometry.py    #   PDF 水印位置与平铺几何（预览与输出共用）
│     ├─ ocr/               #   OCR 双引擎
│     │  ├─ tiling.py       #     自适应切片与跨切片合并（核心算法）
│     │  ├─ table.py        #     从文字框坐标几何重建表格
│     │  ├─ local.py        #     RapidOCR 本地离线引擎
│     │  ├─ deepseek.py     #     DeepSeek Vision 云端引擎
│     │  ├─ router.py       #     引擎选择 / 降级 / 隐私策略
│     │  └─ prompts.py      #     各模式提示词模板
│     ├─ jobs/manager.py    #   任务队列（并发/进度/取消/失败隔离/落库）
│     ├─ storage/           #   SQLite 历史 + 结果缓存 + 参数预设
│     ├─ security/secrets.py#   DPAPI 加密的密钥存储
│     ├─ engines/probe.py   #   引擎能力探测
│     └─ api/               #   路由：system / jobs / preview / settings / presets / 鉴权
├─ packages/shared/         # 前后端共享类型契约（唯一事实来源）
├─ tools/                   # 开发工具
│  ├─ smoke_test.py         #   端到端冒烟测试（起真实内核跑完整流程）
│  ├─ test_packaged.py      #   打包内核验证（真的跑一遍各类任务）
│  ├─ probe.py              #   列出已注册动作与引擎可用性
│  ├─ make_icon.py          #   生成多尺寸应用图标
│  └─ cdp.mjs               #   UI 探针（Chrome DevTools Protocol）
├─ patches/                 # pnpm 补丁（修上游依赖缺陷，见下）
└─ docs/                    # 设计方案与各里程碑验收报告
```

---

## 已实现的关键设计点

- **动作注册表**：所有功能统一表达为「动作」（接收一批文件 + 参数，产出输出文件）。
  新增能力 = 新增一个 `@register` 的函数，队列、进度、重试、结果表全都免费获得；
  UI 读 `paramsSchema` 即可自动生成参数表单，不用改前端。
  多文件进单文件出的功能（如 PDF 合并）用 `aggregate=True` 声明为**聚合动作**。
- **PDF 水印是矢量文字**：任意缩放都清晰、文件极小（4 页仅 2 KB），
  而且**文字仍可被复制与检索**；渲染成位图贴上去用户就选不中了。
  任意角度旋转靠 `morph=(中心点, 旋转矩阵)`；旋转页面则先临时把
  `page.rotation` 归零（实测可见坐标与插入坐标语义不一致，会导致水印落到页外），
  打完再恢复。
- **内核握手协议**：Python 就绪后向 stdout 打印 `@@DOCFORGE_READY@@ {port, token}`，
  Electron 解析后完成连接。端口随机、Token 每次启动重新生成且不落盘。
- **Token 不进渲染进程**：所有内核调用都经主进程 IPC 转发，渲染层即使被注入也拿不到凭证。
- **服务端实时预览**：预览与正式输出**走同一套动作代码**（缩略图 + 相对参数），
  保证所见即所得；若前端用 Canvas 复刻排版逻辑，必然出现"预览好看、结果不对"。
- **失败隔离**：单个坏文件只失败它自己，并在结果表里给出可读原因，绝不拖垮整批。
- **线程池而非进程池**：Pillow / PyMuPDF / ONNX 的重活都释放 GIL，线程已能真实并行；
  进程池需要序列化参数、无法共享字体与模型缓存、取消也不及时。
- **原子写入**：先写临时文件、校验通过再改名，输出目录里出现的文件一定是完整的。
- **EXIF 先摆正再打水印**，并在输出中清除 Orientation 标记（否则看图软件会二次旋转）。
- **启动失败不阻塞 UI**：内核异常时界面照常显示，并在标题栏给出可读原因与重试入口。
- **父进程看门狗**：Electron 被强杀时 Python 内核自动退出，不留孤儿进程。
- **拖拽导入**：`dragenter/dragleave` 用计数器避免遮罩闪烁；
  路径通过 `webUtils.getPathForFile` 获取（Electron 32+ 已移除 `File.path`）。
- **引擎路由与降级**：启动即探测所有引擎，按保真度打分并标注首选；
  某个引擎不可用时自动落到下一档。Office↔PDF 是三级路由：
  **Office COM（保真 100）→ LibreOffice（82）→ 内置渲染（55，永远可用）**。
- **COM 自动化的三条硬约束**（都实测踩过）：
  ① COM 是 STA，因此每个 Office 应用配一条**专用工作线程**串行执行；
  ② 模态对话框会让调用永久挂起，因此每个调用都带**看门狗超时**并强杀重建；
  ③ **强杀时只清理"创建 COM 对象之后新出现的"进程**，用户自己开着的 Word/Excel
  永不触碰（误杀用户未保存的文档是不可接受的事故）。
  另外清理缺陷前后修了**五处**，每一处都有独立成因，缺一处就会漏进程：
  ① `TerminateProcess` 会跳过所有 Python 清理 → 新增优雅退出接口；
  ② `Quit()` 抛异常会跳过 kill → 放进 `finally`；
  ③ **工作线程是守护线程、退出时会被掐断** → 由调用线程兜底强制清理；
  ④ **`_invalidate()` 只丢弃引用、不杀进程** → 每次调用失败都留下一个孤儿
  Office 进程，而且下次 `_get_app()` 会把"启动前已存在的进程"整体记为外来进程，
  那个孤儿从此**任何清理逻辑都认领不到**（实测复现：连续 4 次失败 =
  4 个残留 WINWORD.EXE，`shutdown_all()` 之后依然全部存活）。
  现在改成"先释放代理、再杀进程"，并在 `_get_app()` 里加了一道兜底清理；
  ⑤ **异常 traceback 持有 app 引用** → 异常被存进 Future 后可能在**别的线程**上
  被回收，COM 代理的 `Release()` 便发生在没有 `CoInitialize` 的线程上，
  Windows 抛 `0x800401f0`。现在存入 Future 前先摘掉 traceback，
  并保证最后一个引用在工作线程上、且**服务器还活着**时释放
  （反过来的话 `Release()` 会向已死的服务器发 RPC，抛 `0x800706ba`）。
  这两条都在 `test_doc_convert.py` 里有对应的回归用例。
- **数据目录可降级**：`%APPDATA%` 不可写时自动退到临时目录或工作区并记录原因，
  不让内核因权限问题直接崩溃。
- **流水线就是动作的串联**：`pipeline` 本身也是一个注册动作，每一步直接调用已有
  动作的 handler —— 新注册一个动作就自动能进流水线，不需要在两处各登记一次；
  界面里也是从同一个动作注册表里挑步骤。中间产物写在临时目录并在结束时
  （成败都一样）整棵删掉，用户的输出目录里只会出现最终结果。
  输出扩展名按**最后一步**动态计算（`ActionSpec.output_ext_fn`），
  否则一条以「文档转 PDF」收尾的流水线会产出"内容是 PDF、后缀是 .docx"的文件。
- **参数预设**：把调好的一套参数按动作存进 SQLite，一键套用；同名即覆盖。
  预设只做结构校验、不做参数校验 —— 后端增删参数不会让旧预设变成无法加载的坏数据。

---

## 开发里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| **M0** | 脚手架、内核通信链路、引擎探测、拖拽导入通道、科技风外壳 | ✅ 已完成 |
| **M1** | 设计系统组件库、任务中心 UI | ✅ 已完成 |
| **M2** | 任务队列内核（SQLite + SSE 进度 + 批量结果表 + 历史） | ✅ 已完成 |
| **M3** | 图片水印（服务端实时预览、画布拖拽定位、平铺、每张唯一） | ✅ 已完成 |
| **M4** | OCR 双引擎（DeepSeek Vision 自适应切片 + RapidOCR 本地离线）+ 图片转 Excel + 设置页 | ✅ 已完成 |
| **M5** | PDF 水印（矢量文字、实时预览、平铺、拖拽定位）+ PDF 工具箱（合并/拆分/旋转/压缩/提取页/转图片/文档信息） | ✅ 已完成 |
| **M6** | PDF ↔ Word 双向转换；Word/Excel/PPT → PDF 三级引擎路由（Office COM → LibreOffice → 内置渲染） | ✅ 已完成 |
| **M7** | 批量与自动化：双层可搜索 PDF、命令行 CLI、参数预设、多步骤流水线、监听文件夹 | ✅ 已完成 |
| **M8** | 打磨与打包：PyInstaller 内核 + NSIS 安装包 + 便携版 + 首次启动环境自检 | ✅ 已完成 |
| M9 | 进阶：卡证票据识别、公式识别、PDF 对比、电子签章 | 计划中 |

**共注册 15 个功能动作**：图片加水印、图片提取文字、图片转 Excel、
PDF 加水印/合并/拆分/提取页/旋转/压缩/转图片/文档信息/扫描件转可搜索 PDF、
文档转 PDF、PDF 转 Word，外加把任意动作串起来的**多步骤流水线**。

### 交付产物

| 产物 | 体积 | 说明 |
|---|---|---|
| `文枢 DocForge-0.1.0-x64.exe` | ~218 MB | NSIS 安装包（可选安装目录、快捷方式、文件关联） |
| `文枢 DocForge-便携版-0.1.0.exe` | ~218 MB | 便携版，解压即用、不写注册表 |

在 `apps/desktop/release/` 下，执行 `pnpm package:win` 生成。


详见 `docs/设计方案.md`；已完成里程碑的验收证据见 `docs/` 下的验收报告。

### 关于 OCR 的双引擎

| 引擎 | 特点 | 何时使用 |
|---|---|---|
| **RapidOCR（本地离线）** | 完全离线、零费用、文件不出本机、模型随包分发 | **默认**。隐私敏感、内网、无密钥环境 |
| **DeepSeek Vision（云端）** | 复杂表格、手写、版面理解更强 | 需要更高精度时，在设置中开启并填入 API Key |

云端识别默认**关闭**。开启后仍有隐私护栏：命中「敏感文件名规则」的文件会被强制留在本地。
云端请求带**自适应切片**——按字号判断是否需要切块，避免官方"单图 1300×1300 等效"
的缩放导致密集小字丢失；结果按内容哈希缓存，同一文件重复处理不会重复计费。
