# 文枢 DocForge 0.1.0

> 办公文件转换与处理中枢 · 本地优先（Local-First）· Windows 桌面

把办公里那些"每天重复点一遍"的活收进一个桌面应用，并且**默认不联网**：
照片转 Excel / 提取文字、PDF ↔ Word、PDF 与图片加水印、扫描件转可搜索 PDF、
多步骤流水线批处理，全部可以离线完成；需要更高精度时再按需开启
**DeepSeek 多模态 OCR**。

![总览](https://raw.githubusercontent.com/Stellum-Waq/docforge/main/docs/screenshots/01-dashboard.png)

## 下载

| 文件 | 体积 | 说明 |
|---|---|---|
| `DocForge-0.1.0-Setup-x64.exe` | 217 MiB | **安装版**：可选安装目录，创建桌面/开始菜单快捷方式，关联 PDF / Word / Excel |
| `DocForge-0.1.0-Portable-x64.exe` | 217 MiB | **便携版**：双击即用，不写注册表；用户数据放在 exe 旁的 `DocForge数据/`，U 盘换机也能带走预设与历史 |

两版都已内置 Python 内核与全部依赖（PDF 引擎、OCR 模型、Office 自动化），
**下载后无需再装任何东西**。

> 文件名用 ASCII 是刻意的：中文名在"浏览器 → CDN → 下载工具"这条链路上
> 容易因编码不一致变成乱码或被改名（本仓库发布时就实测踩过一次）。
> 产品名与界面仍然是中文，只有下载下来的文件名是纯 ASCII。

### 校验

```
DocForge-0.1.0-Setup-x64.exe
SHA256  7DA22EC505EFA2E38D2F30D2459CF182B4ABA10101E6FECFA0F1FBF1A8929B3C

DocForge-0.1.0-Portable-x64.exe
SHA256  DD73D28846FA9D01312109E426B98620D47DB42176507D0C7CCED6701ADBC063
```

在 PowerShell 里核对：

```powershell
Get-FileHash "DocForge-0.1.0-Setup-x64.exe" -Algorithm SHA256
```

> 安装包**未做代码签名**，Windows SmartScreen 可能提示"未知发布者"。
> 这是未购买代码签名证书的正常表现；可以用上面的 SHA256 核对文件完整性。

## 这一版能做什么

- **图片 → Excel**：识别图片中的表格并生成带样式的 xlsx，离线引擎用坐标几何重建表格
- **图片 → 文字**：双引擎 OCR。默认 **RapidOCR 本地离线**（文件不出本机、零费用），
  可在设置里开启 **DeepSeek Vision 云端**（复杂表格、手写、版面理解更强）
- **PDF ↔ Word**：双向转换，保留文字、图片、表格
- **Office → PDF**：Word / Excel / PPT 三级引擎路由 ——
  Office COM（保真 100）→ LibreOffice（82）→ 内置渲染（55，永远可用）
- **PDF 工具箱**：合并、拆分、旋转、压缩、提取页面、转图片、文档信息
- **PDF 水印**：矢量文字，任意缩放清晰、文字仍可复制检索；平铺、任意角度、页码范围、拖拽定位、实时预览
- **图片水印**：文字 / Logo、平铺防盗图、每张唯一内容、EXIF 方向自动摆正
- **扫描件转可搜索 PDF**：叠加不可见文字层，外观不变但可搜索、可复制
- **多步骤流水线**：把任意动作串成一条链，开工前先校验整条链的类型匹配
- **参数预设**：把调好的一套参数存下来复用
- **命令行**：`run` / `pipeline` / `watch` / `presets`，供脚本与计划任务调度

## 环境要求

Windows 10 1809 或更高版本（x64）。**无需预装 Office** ——
装了 Office 会用 COM 得到最高保真度，没装则自动降级到内置渲染引擎。

## 已知限制

- **云端 OCR 尚未用真实 API Key 联调**。请求构造与错误处理按官方文档实现并有测试覆盖，
  但没有跑过真实调用 —— 这是当前最主要的验证缺口。
- PDF 加密 / 解密 / 权限设置未实现。
- 「监听文件夹」只在命令行提供（它天然是常驻进程，而内核随应用启停）。
- 仅在 Windows 上验证过。

## 验证情况

- 内核单元测试 **295 项**全部通过
- 端到端冒烟测试 **16 组**全部通过
- 打包内核验证 **11 组**全部通过（含双层 PDF / 预设 / 流水线 / CLI）
- 安装包与便携版均已启动实测：环境自检 12 项通过 / 1 项降级 / 0 项失败，
  关闭后 Electron、Python 内核、Office 进程计数均为 0

完整说明见 [README](https://github.com/Stellum-Waq/docforge#readme)，
设计与验收细节见 [docs](https://github.com/Stellum-Waq/docforge/tree/main/docs)。

---

MIT License
