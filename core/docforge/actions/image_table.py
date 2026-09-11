"""图片 → Excel 动作（对应需求 2「照片转换为 excel」）。

两条实现路径，按引擎自动选择：

* **云端（DeepSeek Vision）** —— 用 ``table`` 模式让模型直接返回结构化 JSON，
  能识别合并单元格，复杂表格效果最好。
* **本地（RapidOCR）** —— 用 :mod:`docforge.ocr.table` 从文字框坐标几何重建表格。
  精度不如云端，但完全离线、零费用，内网与隐私场景可用。

无论走哪条路，产出的都是**带样式的 xlsx**：表头加粗填充、冻结首行、列宽自适应，
而不是一坨纯文本 —— 后者用户还得自己排版，等于没省事。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..ocr import OcrError, OcrOptions, recognize
from ..ocr.table import blocks_to_grid, guess_header_row
from .base import (
    ActionContext,
    ActionError,
    ActionSpec,
    TaskResult,
    atomic_write,
    register,
    summarize_with_warnings,
)

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "engine": {
            "type": "string",
            "enum": ["local", "cloud", "auto"],
            "default": "local",
            "title": "识别引擎",
            "description": "cloud 能识别合并单元格、复杂表格更准；local 完全离线",
        },
        "sheet_mode": {
            "type": "string",
            "enum": ["single", "multi"],
            "default": "single",
            "title": "工作表方式",
            "description": "single=所有表格放一张表；multi=每个表格一张工作表",
        },
        "header_row": {
            "type": "string",
            "enum": ["auto", "none"],
            "default": "auto",
            "title": "表头处理",
            "description": "auto=自动识别表头并套用样式；none=全部当数据行",
        },
        "include_text": {
            "type": "boolean",
            "default": False,
            "title": "附带原文工作表",
            "description": "额外生成一张工作表存放识别出的全文，便于核对",
        },
        "include_source_sheet": {
            "type": "boolean",
            "default": False,
            "title": "附带识别信息",
            "description": "记录使用的引擎、耗时、切片数等，便于排查",
        },
        "tiling": {
            "type": "string",
            "enum": ["auto", "off", "always"],
            "default": "auto",
            "title": "切片策略",
        },
    },
    "required": ["engine"],
}

#: 表头样式
_HEADER_FILL = "1F3B57"
_HEADER_FONT_COLOR = "E8EDF7"
_ALTERNATE_FILL = "F2F6FB"

#: 会被当作货币符号处理的前缀
_CURRENCY_SYMBOLS = ("¥", "￥", "$", "€", "£")

#: 千分位数字的严格形式（用于排除"用逗号分隔的枚举值"这类并非数字的内容）
_THOUSANDS_RE = None


def _thousands_pattern():
    global _THOUSANDS_RE
    if _THOUSANDS_RE is None:
        import re

        _THOUSANDS_RE = re.compile(r"-?\d{1,3}(,\d{3})+(\.\d+)?")
    return _THOUSANDS_RE


def _parse_cell(raw: str) -> tuple[Any, str | None]:
    """把单元格文本解析成 (值, Excel 数字格式)。

    ## 为什么不能只做"转数字"

    最初只把 ``"1,250"`` 转成数字 1250 —— 值对了，但用户在 Excel 里看到的是
    ``1250``，千分位没了；而 ``"¥128,600.00"`` 因为带货币符号转换失败，
    干脆留成了**文本**，那一列连求和都做不到。

    两种表现都算"结果不理想"。正确做法是**值转成数字、同时给单元格套一个
    与原文一致的显示格式**：

        "1,250"       → 1250        + #,##0
        "¥128,600.00" → 128600      + "¥"#,##0.00
        "18.50"       → 18.5        + 0.00
        "12.5%"       → 0.125       + 0.0%

    这样既能参与计算，显示出来又与原件一致 —— 两头都不丢。

    刻意**不**转的情况：带前导零的编号（``007``）、逗号不是千分位的文本
    （``甲,乙``）、以及不含数字的内容，一律保持原文，避免把编号或枚举值改成数字。
    """
    text = raw.strip()
    if not text:
        return raw, None

    # 前导零编号（"007"、"0351"）保持文本，否则编号就毁了
    if len(text) > 1 and text[0] == "0" and text[1].isdigit():
        return raw, None

    body = text
    currency = ""
    for symbol in _CURRENCY_SYMBOLS:
        if symbol in body:
            currency = symbol
            body = body.replace(symbol, "")
            break
    body = body.strip()

    percent = body.endswith("%")
    if percent:
        body = body[:-1].strip()

    # 会计写法：用括号表示负数
    parenthesised = body.startswith("(") and body.endswith(")")
    if parenthesised:
        body = body[1:-1].strip()

    if not body or not any(ch.isdigit() for ch in body):
        return raw, None

    has_thousands = "," in body
    if has_thousands and not _thousands_pattern().fullmatch(body):
        # 逗号不是千分位（更可能是枚举值），保持文本
        return raw, None

    cleaned = body.replace(",", "")
    try:
        number: Any = float(cleaned) if "." in cleaned else int(cleaned)
    except ValueError:
        return raw, None

    if parenthesised:
        number = -abs(number)
    if percent:
        number = number / 100

    decimals = len(cleaned.split(".")[1]) if "." in cleaned else 0
    core = "#,##0" if has_thousands else "0"
    if decimals:
        core += "." + "0" * decimals
    if currency:
        core = f'"{currency}"{core}'
    if percent:
        core += "%"

    if parenthesised:
        negative_core = "#,##0" + ("." + "0" * decimals if decimals else "")
        core = f"{core};{negative_core}"

    return number, core


def _coerce(value: str) -> Any:
    """只取值、不要格式（保留给需要简单转换的调用方）。"""
    return _parse_cell(value)[0]


def _normalize_table(table: dict[str, Any]) -> tuple[list[str], list[list[str]]]:
    """把云端返回的表格对象整理成 (headers, rows)。"""
    headers = [str(cell) if cell is not None else "" for cell in (table.get("headers") or [])]
    raw_rows = table.get("rows") or []

    rows: list[list[str]] = []
    for row in raw_rows:
        if isinstance(row, dict):
            # 容错：模型偶尔把行返回成 {"列名": 值} 形式
            keys = headers or list(row.keys())
            rows.append([str(row.get(key, "")) if row.get(key) is not None else "" for key in keys])
        elif isinstance(row, list):
            rows.append([str(cell) if cell is not None else "" for cell in row])
        else:
            rows.append([str(row)])

    return headers, rows


def _write_sheet(
    worksheet,
    rows: list[list[str]],
    *,
    header_index: int = -1,
) -> None:
    """写入工作表。

    :param rows: **全部**行，按原始顺序。绝不在这里丢弃任何行。
    :param header_index: 需要套用表头样式的行号（0 起，-1 表示没有）。

    为什么用 ``header_index`` 而不是 ``(headers, rows)`` 两个参数：
    早先的实现把"表头之前的行"直接丢掉，结果当表头被误判到别处时
    （文档正文被当成表头是常事），正文就**静默消失**了 ——
    用户只会发现 Excel 里少了几行，却不知道为什么。
    现在先完整写入，再对指定行套样式，任何情况下都不丢内容。
    """
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    thin = Side(style="thin", color="D5DEE9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    if not rows:
        return

    width = max([len(row) for row in rows] + [1])

    for offset, row in enumerate(rows):
        is_header = offset == header_index
        for col in range(1, width + 1):
            cell = worksheet.cell(row=offset + 1, column=col)
            value = row[col - 1] if col - 1 < len(row) else ""
            if is_header:
                # 表头保持原文（避免把 "2026" 之类变成数字影响观感）
                cell.value = value
            else:
                parsed, number_format = _parse_cell(value)
                cell.value = parsed
                if number_format:
                    # 值与显示格式一起给：值能算，显示又和原件一致
                    cell.number_format = number_format
            cell.border = border
            if is_header:
                cell.font = Font(bold=True, color=_HEADER_FONT_COLOR, size=11)
                cell.fill = PatternFill("solid", fgColor=_HEADER_FILL)
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            else:
                # 单元格内换行要能显示出来，否则多行内容会挤成一行
                cell.alignment = Alignment(vertical="top", wrap_text="\n" in str(value))
                if offset % 2 == 1:
                    cell.fill = PatternFill("solid", fgColor=_ALTERNATE_FILL)

    if header_index >= 0:
        # 冻结在表头下方，滚动时表头始终可见
        worksheet.freeze_panes = f"A{header_index + 2}"

    # 列宽自适应：按内容长度估算，限制在 8–48 之间，避免超长文本把表格撑爆
    for col in range(1, width + 1):
        longest = 0
        for row in rows:
            if col - 1 < len(row):
                text = str(row[col - 1])
                # 中文按两个字符宽度计
                longest = max(longest, sum(2 if ord(ch) > 0x2E80 else 1 for ch in text))
        worksheet.column_dimensions[get_column_letter(col)].width = min(48, max(8, longest + 3))


def _coerce(value: str) -> Any:
    """把看起来像数字的字符串转成数值。保留原始文本兜底。"""
    text = value.strip()
    if not text:
        return value
    # 去掉千分位后尝试转数字，但保留前导零的编号（如 "007"）不转
    candidate = text.replace(",", "")
    if candidate.startswith("0") and len(candidate) > 1 and candidate[1].isdigit():
        return value
    try:
        if "." in candidate:
            return float(candidate)
        return int(candidate)
    except ValueError:
        return value


def _build_workbook(
    path: Path,
    tables: list[tuple[str, list[list[str]], int]],
    *,
    text: str,
    include_text: bool,
    meta: dict[str, Any],
    include_meta: bool,
) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.remove(workbook.active)

    if not tables:
        # 没识别到表格也别产出空文件：把全文按单列写入，用户至少能拿去加工
        worksheet = workbook.create_sheet("识别结果")
        worksheet.cell(row=1, column=1, value="未识别到表格结构，以下为全文")
        for index, line in enumerate(text.splitlines(), start=2):
            worksheet.cell(row=index, column=1, value=line)
        worksheet.column_dimensions["A"].width = 60
    elif len(tables) == 1:
        title, rows, header_index = tables[0]
        worksheet = workbook.create_sheet(title=_safe_sheet_name(title) or "Sheet1")
        _write_sheet(worksheet, rows, header_index=header_index)
    else:
        for index, (title, rows, header_index) in enumerate(tables, start=1):
            name = _safe_sheet_name(title) or f"表格{index}"
            worksheet = workbook.create_sheet(title=name)
            _write_sheet(worksheet, rows, header_index=header_index)

    if include_text and text.strip():
        sheet = workbook.create_sheet(title="原文")
        sheet.cell(row=1, column=1, value="识别全文")
        for index, line in enumerate(text.splitlines(), start=2):
            sheet.cell(row=index, column=1, value=line)
        sheet.column_dimensions["A"].width = 80

    if include_meta:
        sheet = workbook.create_sheet(title="识别信息")
        for index, (key, value) in enumerate(meta.items(), start=1):
            sheet.cell(row=index, column=1, value=key)
            sheet.cell(row=index, column=2, value=str(value))
        sheet.column_dimensions["A"].width = 20
        sheet.column_dimensions["B"].width = 60

    if not workbook.sheetnames:
        workbook.create_sheet(title="Sheet1")

    workbook.save(path)


def _safe_sheet_name(name: str) -> str:
    """Excel 工作表名有字符与长度限制，必须清洗。"""
    cleaned = "".join(ch for ch in (name or "") if ch not in '[]:*?/\\').strip()
    return cleaned[:28]


def _handler(ctx: ActionContext) -> TaskResult:
    source = Path(ctx.file_path)
    engine_pref = ctx.str_param("engine", "local")

    # 云端走 table 模式拿结构化 JSON；本地走 text 模式拿坐标自己重建
    use_cloud = engine_pref in ("cloud", "auto")
    mode = "table" if use_cloud else "text"

    options = OcrOptions(
        mode=mode,
        language="auto",
        tiling=ctx.str_param("tiling", "auto"),
        extract_tables=True,
    )

    ctx.report(2, "准备识别")

    try:
        result = recognize(
            source,
            options,
            preference=engine_pref,
            progress=lambda value, message: ctx.report(2 + value * 0.8, message),
        )
    except OcrError as err:
        raise ActionError(str(err)) from err

    ctx.raise_if_cancelled()
    ctx.report(85, "生成表格")

    # 表格统一表示为 (标题, 完整行列表, 表头行号)。
    # 刻意保存**完整行列表**而不是拆成 headers/rows 两份：
    # 拆开再拼回去的过程正是当初丢数据的地方。
    tables: list[tuple[str, list[list[str]], int]] = []
    header_mode = ctx.str_param("header_row", "auto")

    # 优先用云端返回的结构化表格
    for item in result.raw.get("tables") or []:
        if not isinstance(item, dict):
            continue
        headers, rows = _normalize_table(item)
        if headers or rows:
            has_header = bool(headers) and any(h.strip() for h in headers)
            all_rows = ([headers] if has_header else []) + rows
            index = 0 if (has_header and header_mode == "auto") else -1
            tables.append((str(item.get("title") or ""), all_rows, index))

    used_grid_fallback = False
    if not tables and result.blocks:
        # 本地引擎（或云端未识别到表格）走几何重建
        grid = blocks_to_grid(result.blocks)
        if grid.row_count > 0:
            header_index = guess_header_row(grid.rows) if header_mode == "auto" else -1
            tables.append(("", grid.rows, header_index))
            used_grid_fallback = True

    if not tables:
        raise ActionError("未识别到任何内容，请确认图片清晰且包含表格或文本")

    target = Path(ctx.output_path)
    if target.suffix.lower() != ".xlsx":
        target = target.with_suffix(".xlsx")
        ctx.output_path = str(target)

    meta = {
        "源文件": source.name,
        "识别引擎": result.engine,
        "图片尺寸": f"{result.width}×{result.height}",
        "切片数": result.tiles,
        "识别行数": len(result.blocks),
        "耗时(ms)": result.elapsed_ms,
        "表格数": len(tables),
        "云端用量": result.usage or "—",
    }

    try:
        atomic_write(
            target,
            lambda tmp: _build_workbook(
                tmp,
                tables,
                text=result.text,
                include_text=ctx.bool_param("include_text", False),
                meta=meta,
                include_meta=ctx.bool_param("include_source_sheet", False),
            ),
        )
    except ActionError:
        raise
    except OSError as err:
        raise ActionError(f"写入 Excel 失败：{err}") from err

    ctx.report(100, "完成")

    row_total = sum(len(rows) for _, rows, _ in tables)
    summary = f"{len(tables)} 张表 · {row_total} 行"
    if used_grid_fallback:
        summary += " · 坐标重建"

    # 引擎的提醒必须传出去：模型认不清时会"编"出看似合理的内容，
    # 用户看到绿色的成功就不会再复查，所以警告不能只留在内部对象里。
    return TaskResult(
        output_path=str(target),
        message=summarize_with_warnings(ctx, summary, result.warnings),
    )


register(
    ActionSpec(
        id="image.to_excel",
        label="图片转 Excel",
        domain="ocr",
        handler=_handler,
        accepts=("jpg", "jpeg", "png", "bmp", "webp", "tif", "tiff", "heic", "heif", "avif"),
        output_ext="xlsx",
        description="识别图片中的表格并生成带样式的 Excel；离线引擎用坐标几何重建，云端引擎可识别合并单元格",
        params_schema=PARAMS_SCHEMA,
    )
)
