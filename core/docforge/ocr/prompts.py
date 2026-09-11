"""云端 OCR 的提示词模板。

## 为什么提示词单独成模块

OCR 的准确率对提示词极其敏感，而且不同场景（纯文字 / 表格 / 公式 / 卡证）需要
完全不同的输出契约。把提示词集中在一处，好处是：
  * 调整提示词不需要碰任何业务代码
  * 提示词版本号参与结果缓存键，改了提示词旧缓存自动失效（否则用户会拿到
    按旧提示词产生的结果，却以为新提示词没生效）

## 一条硬性约束

DeepSeek 的 JSON 模式规定：**提示词里必须出现 "json" 字样**，否则接口会报错。
所有 JSON 类模板都遵守这一点。
"""

from __future__ import annotations

#: 提示词版本。任何模板改动都必须提升它，否则旧缓存会被错误复用。
PROMPT_VERSION = "v1"

#: 所有模式共用的基础约束。经验上这几句话对"模型爱加解说"的毛病最有效。
_COMMON_RULES = """要求：
1. 逐字识别，**不要**翻译、改写、纠错、补全或总结。
2. **只输出内容本身**，不要任何开场白、说明、解释或结尾客套。
3. 保持原始阅读顺序（多栏排版按栏读完再读下一栏）。
4. 保留原有的换行与分段。
5. 无法辨认的字符用 "□" 占位，不要猜测。
6. 不要输出 Markdown 代码块标记（除非明确要求）。"""

TEXT_PROMPT = """请识别这张图片中的全部文字。

{common}
"""

TEXT_PROMPT_WITH_LANG = """请识别这张图片中的全部文字（主要语言：{language}）。

{common}
"""

#: 版面还原：保留标题层级、列表、表格等结构，输出 Markdown
LAYOUT_PROMPT = """请把这张图片还原成结构化 Markdown 文档。

要求：
1. 逐字识别，不要润色、改写或总结内容。
2. 用 Markdown 表达版面结构：
   - 标题用 #、##、###（按视觉字号层级判断）
   - 无序列表用 -，有序列表用 1. 2. 3.
   - 表格用标准 Markdown 表格语法
   - 正文段落之间保留空行
3. **只输出 Markdown 正文**，不要加 ``` 代码块包裹，不要任何说明。
4. 无法辨认的字符用 "□" 占位。
"""

#: 表格识别：直接要 JSON，比让模型画 Markdown 表格再由正则解析可靠得多
TABLE_PROMPT = """请识别这张图片中的表格，并以 json 格式返回。

严格按以下 json 结构输出，不要添加任何解释文字：
{{
  "tables": [
    {{
      "title": "表格标题，没有则留空字符串",
      "headers": ["表头1", "表头2"],
      "rows": [
        ["第一行第一列", "第一行第二列"],
        ["第二行第一列", "第二行第二列"]
      ],
      "spans": [
        {{"row": 0, "col": 0, "rowSpan": 1, "colSpan": 2}}
      ]
    }}
  ],
  "text": "表格之外的普通文字，按阅读顺序拼接"
}}

要求：
1. 逐字识别，不要改写、补全或计算数值。
2. `rows` 中每一行的列数必须与 `headers` 一致；缺失的单元格用空字符串填充。
3. 合并单元格用 `spans` 描述（可选，没有就返回空数组）。行列索引从 0 开始。
4. 数字保持原样，包括千分位与货币符号（例如 "¥1,234.56"）。
5. 如果图中没有表格，`tables` 返回空数组，把内容放进 `text`。
"""

#: 公式识别：转 LaTeX
FORMULA_PROMPT = """请识别这张图片中的数学公式，转换为 LaTeX。

要求：
1. 行内公式用 $...$，独立公式用 $$...$$。
2. **只输出 LaTeX**，不要解释、不要代码块包裹。
3. 同时给出公式的纯文本读法，放在 LaTeX 之后，用一行 "---" 分隔。
"""

#: 卡证票据结构化（对应设计文档 §1-A5）
CARD_PROMPT = """请识别这张卡证/票据图片，以 json 格式返回结构化字段。

严格按以下 json 结构输出，不要添加任何解释文字：
{{
  "documentType": "身份证|银行卡|营业执照|增值税发票|收据|名片|其他",
  "fields": {{"字段名": "字段值"}},
  "text": "完整原始文字"
}}

要求：
1. 字段名用中文，尽量覆盖图中出现的所有关键信息（如"姓名""证件号码""金额""开票日期"）。
2. 逐字识别，号码类字段保留原始格式（含空格与横线）。
3. 不要输出图片中没有的字段。


"""

#: 混合模式的"语义切块"指令：把版面结构用 JSON 描述出来，供切片使用
LAYOUT_JSON_PROMPT = """请分析这张文档图片的版面结构，以 json 格式返回。

严格按以下 json 结构输出，不要添加任何解释文字：
{{
  "blocks": [
    {{"type": "title|text|table|figure|header|footer", "x0": 0, "y0": 0, "x1": 100, "y1": 100, "note": ""}}
  ]
}}

要求：
1. 坐标使用**相对比例**（0 到 1 之间的小数，相对图片宽高），便于不同分辨率复用。
2. 按阅读顺序输出各个区块，区块之间不要重叠。
3. 表格整体作为一个 table 区块，不要把表格拆成多行。
4. 如果整页是均匀正文，返回一个覆盖全图的 text 区块即可。
"""


def build_prompt(mode: str, *, language: str = "auto", override: str | None = None) -> str:
    """按模式组装提示词。

    :param mode: text / layout / table / formula / card / layout_json
    """
    if override:
        return override

    if mode == "table":
        return TABLE_PROMPT

    if mode == "layout":
        return LAYOUT_PROMPT

    if mode == "formula":
        return FORMULA_PROMPT

    if mode == "card":
        return CARD_PROMPT

    if mode == "layout_json":
        return LAYOUT_JSON_PROMPT

    if language and language not in ("auto", ""):
        return TEXT_PROMPT_WITH_LANG.format(language=language, common=_COMMON_RULES)

    return TEXT_PROMPT.format(common=_COMMON_RULES)


#: 哪些模式要求模型返回 JSON（决定是否开启 response_format）
JSON_MODES = frozenset({"table", "card", "layout_json"})
