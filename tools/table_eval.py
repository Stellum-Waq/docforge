"""图片转 Excel 的评测台（开发工具）。

## 为什么需要它

"识别效果不理想"是一句无法迭代的话 —— 必须先把"不理想"变成可比对的数字，
否则每次改提示词都只是在凭感觉猜。这个脚本做三件事：

1. **按已知的二维数组渲染测试图** —— 因此标准答案是精确的，不靠人工标注；
2. **走真实动作链路**（``image.to_excel`` 的 handler）而不是只调引擎，
   量的才是用户真正拿到的东西；
3. **逐格比对**，输出单元格级准确率、多出来的行/列、缺失的行/列。

用法::

    python tools/table_eval.py                      # 跑全部用例
    python tools/table_eval.py --case 标准有框表
    python tools/table_eval.py --engine local       # 对比本地引擎
    python tools/table_eval.py --dump 标准有框表    # 打印模型返回的原始 JSON
    python tools/table_eval.py --keep               # 保留中间产物便于查看

注意：云端结果有缓存（用"文件内容 + 提示词版本 + 参数"做键），
所以每次评测会**先清空 OCR 缓存**，否则量到的是上一版提示词的结果。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "core"))

FONT_PATH = "C:/Windows/Fonts/msyh.ttc"
WORK = REPO / ".cache" / "table-eval"


# --------------------------------------------------------------------------- #
# 用例定义                                                                      #
# --------------------------------------------------------------------------- #

@dataclass
class Case:
    name: str
    grid: list[list[str]]
    style: str = "grid"  # grid=有框线 / plain=无框线 / merged=含合并单元格
    title: str = ""
    font_size: int = 28
    #: 期望的表头行号（0 起）；-1 表示没有表头
    header_index: int = 0
    #: 合并区域 (r0, c0, r1, c1)，闭区间
    merges: list[tuple[int, int, int, int]] = field(default_factory=list)
    #: 模拟拍照：透视变形 + 光照不均 + 噪声 + JPEG 压缩
    photo: bool = False
    #: 模拟手机拍的倾斜角度（度）
    skew: float = 0.0
    #: 把表格贴到更大的画布上。字号不变、画布变大 → 文字相对整图变小，
    #: 这正是"远距离拍一整页 / 高分辨率扫小字"的情形，会真正触发切片。
    canvas: tuple[int, int] | None = None


def _dense_rows(rows: int) -> list[list[str]]:
    """造一张够大够密的表，用来逼出切片路径。"""
    header = ["序号", "产品名称", "规格型号", "数量", "单价", "金额", "备注"]
    names = [
        "复印纸", "签字笔", "档案盒", "订书机", "文件夹", "便签纸", "胶带",
        "打印机墨盒", "白板笔", "计算器", "印章", "封箱胶带",
    ]
    grid = [header]
    for index in range(rows):
        qty = 12 + index * 7
        price = 8.5 + index * 1.35
        grid.append(
            [
                str(index + 1),
                names[index % len(names)],
                f"DF-{1000 + index * 7}",
                f"{qty:,}",
                f"{price:.2f}",
                f"{qty * price:,.2f}",
                "" if index % 4 else "急需",
            ]
        )
    return grid


CASES: list[Case] = [
    Case(
        name="标准有框表",
        grid=[
            ["部门", "一季度", "二季度", "三季度", "四季度", "全年合计"],
            ["技术部", "1,250", "1,380", "1,410", "1,520", "5,560"],
            ["市场部", "980", "1,120", "1,050", "1,260", "4,410"],
            ["财务部", "640", "610", "680", "720", "2,650"],
            ["人事部", "420", "390", "430", "460", "1,700"],
            ["合计", "3,290", "3,500", "3,570", "3,960", "14,320"],
        ],
    ),
    Case(
        name="带标题与金额",
        grid=[
            ["项目名称", "合同金额", "已付金额", "未付金额", "付款日期"],
            ["办公设备采购", "¥128,600.00", "¥90,000.00", "¥38,600.00", "2026-03-15"],
            ["系统开发服务", "¥560,000.00", "¥420,000.00", "¥140,000.00", "2026-06-30"],
            ["场地租赁", "¥240,000.00", "¥240,000.00", "¥0.00", "2026-01-05"],
            ["员工培训", "¥36,800.00", "¥18,400.00", "¥18,400.00", "2026-09-20"],
        ],
        title="2026 年度合同付款台账",
    ),
    Case(
        name="合并单元格",
        grid=[
            ["类别", "姓名", "岗位", "入职日期", "考核结果"],
            ["技术中心", "张伟", "后端工程师", "2023-03-01", "优秀"],
            ["", "李娜", "前端工程师", "2023-07-15", "良好"],
            ["", "王强", "测试工程师", "2024-01-08", "良好"],
            ["市场中心", "刘洋", "渠道经理", "2022-11-20", "优秀"],
            ["", "陈静", "品牌专员", "2024-05-06", "合格"],
        ],
        style="merged",
        merges=[(1, 0, 3, 0), (4, 0, 5, 0)],
    ),
    Case(
        name="无框线表",
        grid=[
            ["产品型号", "规格", "库存", "单价"],
            ["DF-1001", "A4 80g", "1,240", "18.50"],
            ["DF-1002", "A3 80g", "860", "32.00"],
            ["DF-2001", "A4 70g", "2,050", "15.80"],
            ["DF-2002", "B5 80g", "430", "22.40"],
        ],
        style="plain",
    ),
    Case(
        name="单元格内换行",
        grid=[
            ["序号", "问题描述", "责任人", "状态"],
            ["1", "打印后\n页面出现条纹", "张伟", "已解决"],
            ["2", "批量转换\n偶发超时", "李娜", "处理中"],
            ["3", "导出文件名\n含非法字符", "王强", "已解决"],
        ],
    ),
    # ---- 以下是"难例"：真实办公场景里最容易翻车的几类 ----
    Case(
        name="密集大表",
        grid=_dense_rows(22),
        font_size=20,
    ),
    Case(
        name="宽表截图",
        grid=[
            ["月份", "华东", "华南", "华北", "西南", "东北", "西北", "华中", "海外"],
            ["1 月", "1,204", "980", "766", "512", "430", "388", "655", "212"],
            ["2 月", "1,088", "1,042", "701", "488", "402", "356", "600", "268"],
            ["3 月", "1,356", "1,180", "845", "560", "470", "402", "712", "295"],
            ["4 月", "1,420", "1,096", "790", "602", "455", "388", "690", "310"],
        ],
        font_size=24,
    ),
    Case(
        name="拍照照片",
        grid=[
            ["姓名", "部门", "基本工资", "绩效", "实发合计"],
            ["张伟", "技术部", "12,800.00", "3,200.00", "16,000.00"],
            ["李娜", "市场部", "11,500.00", "2,600.00", "14,100.00"],
            ["王强", "财务部", "10,900.00", "2,100.00", "13,000.00"],
            ["刘洋", "人事部", "10,200.00", "1,900.00", "12,100.00"],
        ],
        title="员工薪酬明细表",
        photo=True,
    ),
    Case(
        name="远距大图切片",
        grid=_dense_rows(26),
        font_size=22,
        canvas=(2400, 3200),
        photo=True,
    ),
    Case(
        # 真实场景对照：手机拍 A4 表格时，纸面基本占满取景框。
        # A4 宽 210mm、12pt 字约 4.2mm → 拍摄宽度 3000px 时字高约 60px，
        # 即使被模型缩到 0.32 也还有 19px，是完全可读的。
        # 用它来证明"远距大图"那个难例是**极端情形**，而非普遍情况。
        name="手机拍A4（纸面占满）",
        grid=_dense_rows(18),
        font_size=46,
        canvas=(3000, 4000),
        photo=True,
    ),
]


# --------------------------------------------------------------------------- #
# 渲染测试图                                                                    #
# --------------------------------------------------------------------------- #

def render(case: Case, target: Path) -> Path:
    """按二维数组渲染测试图。刻意不画多余装饰，避免评测被无关因素干扰。"""
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(FONT_PATH, case.font_size)
    header_font = ImageFont.truetype(FONT_PATH, case.font_size)
    title_font = ImageFont.truetype(FONT_PATH, int(case.font_size * 1.35))

    cols = max(len(row) for row in case.grid)
    rows = len(case.grid)

    pad = 18
    line_h = int(case.font_size * 1.9)
    title_h = int(case.font_size * 2.4) if case.title else 0

    # 列宽按内容实测，中文按全角计
    def text_width(text: str, f) -> int:
        return int(f.getlength(text))

    widths: list[int] = []
    for col in range(cols):
        widest = 0
        for row in case.grid:
            if col < len(row):
                for line in row[col].split("\n"):
                    widest = max(widest, text_width(line, font))
        widths.append(widest + pad * 2)

    height = title_h + rows * line_h + pad * 2
    width = sum(widths) + pad * 2

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)

    y = pad
    if case.title:
        draw.text((pad, y + 6), case.title, font=title_font, fill="black")
        y += title_h

    xs = [pad]
    for w in widths:
        xs.append(xs[-1] + w)

    for index, row in enumerate(case.grid):
        top = y + index * line_h
        bottom = top + line_h
        if case.style == "grid":
            draw.rectangle([xs[0], top, xs[-1], bottom], outline=(60, 60, 60), width=2)
            for x in xs[1:-1]:
                draw.line([(x, top), (x, bottom)], fill=(60, 60, 60), width=2)
        if index == case.header_index and case.header_index >= 0:
            draw.rectangle([xs[0] + 1, top + 1, xs[-1] - 1, bottom - 1], fill=(226, 232, 240))

        for col, cell in enumerate(row):
            if not cell:
                continue
            lines = cell.split("\n")
            block_h = len(lines) * (case.font_size * 1.35)
            text_y = top + (line_h - block_h) / 2 + case.font_size * 0.18
            for line_index, line in enumerate(lines):
                draw.text((xs[col] + pad, text_y + line_index * case.font_size * 1.35), line, font=font, fill="black")

    # 合并单元格：抹掉内部竖线，让视觉上确实是一格
    for (r0, c0, r1, c1) in case.merges:
        top = y + r0 * line_h + 1
        bottom = y + (r1 + 1) * line_h - 1
        left = xs[c0] + 1
        right = xs[c1 + 1] - 1
        draw.rectangle([left, top, right, bottom], fill="white")
        draw.rectangle([left, top, right, bottom], outline=(60, 60, 60), width=2)
        draw.text(
            (left + pad, top + (bottom - top - case.font_size) / 2),
            case.grid[r0][c0],
            font=font,
            fill="black",
        )

    target.parent.mkdir(parents=True, exist_ok=True)

    if case.canvas:
        # 贴到大画布上：字号不变、整图变大 —— 模拟"远距离拍一整页"
        from PIL import Image as _Image

        wide, tall = case.canvas
        board = _Image.new("RGB", (wide, tall), "white")
        board.paste(image, ((wide - image.width) // 2, int(tall * 0.06)))
        image = board

    if case.photo:
        image = _photograph(image, case.skew)

    image.save(target)
    return target


def _photograph(image, skew: float = 0.0):
    """把干净的表格图变成"手机拍的"：透视变形 + 光照不均 + 噪声 + JPEG 压缩。

    为什么必须模拟这个：干净截图上的识别率再高，也不能说明用户拿手机拍一张
    纸质表格时的效果。真实照片有四个干净图没有的麻烦——
    透视畸变（列不再垂直）、光照不均（一边亮一边暗）、传感器噪声、
    以及微信/邮件转发带来的 JPEG 压缩。任何一个都可能把细线和小字吃掉。
    """
    import random

    from PIL import Image, ImageEnhance, ImageFilter

    width, height = image.size

    # 1) 透视变形：四角各内缩一点，模拟斜着拍
    pinch_x = width * (0.035 + abs(skew) * 0.002)
    pinch_y = height * (0.045 + abs(skew) * 0.002)
    quad = (
        0 + pinch_x, 0 + pinch_y * 0.4,
        width - pinch_x * 0.6, 0,
        width, height - pinch_y * 0.5,
        0 + pinch_x * 0.4, height,
    )
    warped = image.transform(
        (width, height), Image.Transform.QUAD, quad, resample=Image.Resampling.BICUBIC
    )

    # 2) 光照不均：左上亮、右下暗的渐变叠加
    gradient = Image.new("L", (width, height))
    pixels = gradient.load()
    for y in range(height):
        for x in range(0, width, 4):
            value = int(255 * (0.62 + 0.38 * (1 - (x / width * 0.6 + y / height * 0.4))))
            for dx in range(4):
                if x + dx < width:
                    pixels[x + dx, y] = value
    shaded = Image.composite(warped, Image.new("RGB", (width, height), (60, 60, 60)), gradient)

    # 3) 噪声 + 轻微模糊（对焦不完美）
    shaded = shaded.filter(ImageFilter.GaussianBlur(0.6))
    pixels = shaded.load()
    rng = random.Random(20260911)
    for _ in range(int(width * height * 0.01)):
        x, y = rng.randrange(width), rng.randrange(height)
        r, g, b = pixels[x, y]
        noise = rng.randint(-26, 26)
        pixels[x, y] = (
            max(0, min(255, r + noise)),
            max(0, min(255, g + noise)),
            max(0, min(255, b + noise)),
        )

    shaded = ImageEnhance.Contrast(shaded).enhance(0.94)
    return shaded


# --------------------------------------------------------------------------- #
# 比对                                                                          #
# --------------------------------------------------------------------------- #

def normalize(text: object) -> str:
    """归一化后比较：全角转半角、去空白、统一大小写。"""
    value = "" if text is None else str(text)
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("\n", " ").replace("\u3000", " ")
    return " ".join(value.split()).strip().lower()


def value_key(text: object) -> str:
    """按"值"比对时用的键：数字只比大小，不比写法。

    `1,250` 与 `1250`、`18.50` 与 `18.5` 在 Excel 里是**同一个值**，
    前者被写成数字反而更正确（文本型的 "1,250" 没法参与求和）。
    所以值准确率要把它们判为一致，否则指标会一直在惩罚正确的行为。

    纯文本同样忽略空白：`1 月` 与 `1月` 对使用者是同一件事，
    盯着这个差异只会淹没真正的内容错误。**格式保真**那一项仍然严格比对写法。
    """
    raw = "" if text is None else str(text)
    compact = (
        unicodedata.normalize("NFKC", raw)
        .replace(",", "")
        .replace("¥", "")
        .replace("￥", "")
        .strip()
    )
    compact = "".join(compact.split())
    try:
        return f"num:{float(compact):.10g}"
    except ValueError:
        return "txt:" + compact.lower()


def format_key(text: object) -> str:
    """按"写法"比对时用的键：千分位与小数位都必须原样保留。"""
    raw = "" if text is None else str(text)
    return " ".join(unicodedata.normalize("NFKC", raw).replace("\n", " ").split()).strip()


def sheet_to_grid(path: Path) -> list[list[list[dict[str, str]]]]:
    """把 xlsx 读成 [sheet][row][cell]，每格带值与数字格式。

    刻意连 `number_format` 一起读：值对了但显示成 ``1250`` 而不是 ``1,250``，
    对办公表格来说仍然是"结果不理想"，所以格式必须进指标。
    """
    from openpyxl import load_workbook

    workbook = load_workbook(path, data_only=True)
    sheets: list[list[list[dict[str, str]]]] = []
    for sheet in workbook.worksheets:
        grid: list[list[dict[str, str]]] = []
        for row in sheet.iter_rows():
            cells = []
            for cell in row:
                value = cell.value
                cells.append(
                    {
                        "value": "" if value is None else str(value),
                        "fmt": cell.number_format or "",
                    }
                )
            grid.append(cells)
        while grid and not any(normalize(c["value"]) for c in grid[-1]):
            grid.pop()
        sheets.append(grid)
    return sheets


def _display_matches(source: str, cell: dict[str, str]) -> bool:
    """源的"写法"与单元格的"值 + 数字格式"是否一致。

    不去完整模拟 Excel 的格式化引擎，而是比对那些**用户看得见**的特征：
    千分位、小数位数、货币符号、百分号。这四个正是办公表格里会被一眼看出来的差别。
    """
    raw = unicodedata.normalize("NFKC", "" if source is None else str(source)).strip()
    fmt = cell["fmt"]

    wants_thousands = "," in raw
    has_thousands = "#,##0" in fmt
    if wants_thousands != has_thousands:
        return False

    wants_decimals = len(raw.split(".")[1]) if "." in raw and raw.split(".")[1].isdigit() else 0
    decimals = 0
    if "." in fmt:
        tail = fmt.split(".")[1]
        decimals = len(tail) - len(tail.lstrip("0"))
    if wants_decimals != decimals:
        return False

    wants_currency = any(sym in raw for sym in ("¥", "￥", "$", "€", "£"))
    has_currency = any(sym in fmt for sym in ("¥", "￥", "$", "€", "£"))
    if wants_currency != has_currency:
        return False

    wants_percent = raw.endswith("%")
    has_percent = fmt.strip().endswith("%")
    return wants_percent == has_percent


def compare(expected: list[list[str]], actual: list[list[dict[str, str]]]) -> dict[str, object]:
    """逐格比对：同时给出「值准确率」与「显示格式保真」。”"""
    exp_rows = len(expected)
    exp_cols = max(len(r) for r in expected)
    act_rows = len(actual)
    act_cols = max((len(r) for r in actual), default=0)

    matched = 0
    exact = 0
    total = exp_rows * exp_cols
    missing: list[str] = []
    wrong: list[str] = []
    format_loss: list[str] = []

    for r in range(exp_rows):
        for c in range(exp_cols):
            want = expected[r][c] if c < len(expected[r]) else ""
            cell = (
                actual[r][c]
                if r < act_rows and c < len(actual[r])
                else {"value": "", "fmt": ""}
            )
            got = cell["value"]

            if value_key(want) == value_key(got):
                matched += 1
                if _display_matches(want, cell):
                    exact += 1
                elif normalize(want) != normalize(got):
                    format_loss.append(f"R{r + 1}C{c + 1}「{want}」显示为「{got}」")
            elif not normalize(got):
                missing.append(f"R{r + 1}C{c + 1} 缺「{want}」")
            else:
                wrong.append(f"R{r + 1}C{c + 1} 期望「{want}」实际「{got}」")

    return {
        "accuracy": matched / total if total else 0.0,
        "formatFidelity": exact / total if total else 0.0,
        "matched": matched,
        "exact": exact,
        "total": total,
        "expectedShape": f"{exp_rows}×{exp_cols}",
        "actualShape": f"{act_rows}×{act_cols}",
        "missing": missing[:8],
        "wrong": wrong[:8],
        "formatLoss": format_loss[:8],
        "extraRows": max(0, act_rows - exp_rows),
        "extraCols": max(0, act_cols - exp_cols),
    }


# --------------------------------------------------------------------------- #
# 主流程                                                                        #
# --------------------------------------------------------------------------- #

def run_case(case: Case, args: argparse.Namespace) -> dict[str, object]:
    from docforge.actions import ActionContext, get_action

    image = render(case, WORK / "images" / f"{case.name}.png")
    out_dir = WORK / "out" / case.name
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{case.name}.xlsx"

    spec = get_action("image.to_excel")
    ctx = ActionContext(
        job_id="eval",
        task_id="t",
        file_path=str(image),
        output_path=str(target),
        params={
            "engine": args.engine,
            "header_row": "auto",
            "tiling": args.tiling,
            "include_source_sheet": True,
        },
    )
    result = spec.handler(ctx)
    produced = Path(result.output_path or target)

    report: dict[str, object] = {
        "case": case.name,
        "image": image.name,
        "message": result.message,
        "outputSize": produced.stat().st_size if produced.is_file() else 0,
    }

    # 引擎给出的提醒会进 message（动作层用 summarize_with_warnings 透传）。
    # 必须露出来 —— 尤其是"字号太小、结果可能不准"这类，
    # 它是"我们没有静默交付垃圾"的证据：否则评测者只看到一个低分，
    # 却不知道程序其实已经警告过用户了。
    if result.message and "⚠" in result.message:
        report["warnings"] = result.message

    if args.dump:
        report["dump"] = dump_raw(case, image, args)

    if not produced.is_file():
        report["error"] = "没有产出 xlsx"
        return report

    sheets = sheet_to_grid(produced)
    report["sheets"] = [
        f"{len(s)}行×{max((len(r) for r in s), default=0)}列" for s in sheets
    ]

    # 用第一张表（非"识别信息"）做比对
    data_sheets = [s for s in sheets if s]
    if data_sheets:
        report.update(compare(case.grid, data_sheets[0]))

    if args.verbose and data_sheets:
        report["actual"] = [
            " | ".join(str(c["value"]) for c in row) for row in data_sheets[0][:12]
        ]

    return report


def dump_raw(case: Case, image: Path, args: argparse.Namespace) -> object:
    """打印引擎返回的原始 JSON，用于判断"是模型没认对"还是"我们没接住"。"""
    from docforge.ocr import OcrOptions, recognize

    result = recognize(
        image,
        OcrOptions(mode="table", tiling=args.tiling),
        preference=args.engine,
    )
    return {
        "engine": result.engine,
        "tiles": result.tiles,
        "warnings": result.warnings,
        "usage": result.usage,
        "tables": result.raw.get("tables"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="图片转 Excel 评测台")
    parser.add_argument("--case", action="append", help="只跑指定用例（可重复）")
    parser.add_argument("--engine", default="cloud", choices=["cloud", "local", "auto"])
    parser.add_argument("--tiling", default="auto", choices=["auto", "off", "always"])
    parser.add_argument("--dump", action="store_true", help="打印引擎返回的原始 JSON")
    parser.add_argument("--verbose", "-v", action="store_true", help="打印实际表格内容")
    parser.add_argument("--keep", action="store_true", help="保留中间产物")
    parser.add_argument("--no-clear-cache", action="store_true", help="不清缓存（默认会清）")
    args = parser.parse_args()

    if not Path(FONT_PATH).is_file():
        print(f"缺少字体 {FONT_PATH}")
        return 2

    # 云端结果有缓存；不清的话量到的是上一版提示词的结果
    if args.engine != "local" and not args.no_clear_cache:
        from docforge.storage.cache import get_sync_cache

        get_sync_cache().clear()

    if WORK.exists() and not args.keep:
        shutil.rmtree(WORK, ignore_errors=True)

    selected = [c for c in CASES if not args.case or c.name in args.case]
    if not selected:
        print(f"没有匹配的用例。可用：{[c.name for c in CASES]}")
        return 2

    print("=" * 78)
    print(f"图片转 Excel 评测 · 引擎={args.engine} · 切片={args.tiling} · {len(selected)} 个用例")
    print("=" * 78)

    reports = []
    for case in selected:
        print(f"\n## {case.name}")
        try:
            report = run_case(case, args)
        except Exception as err:  # noqa: BLE001 - 评测脚本要把失败也记下来
            print(f"   执行失败：{type(err).__name__}: {err}")
            reports.append({"case": case.name, "error": str(err), "accuracy": 0.0})
            continue

        if "error" in report:
            print(f"   {report['error']}")
        else:
            acc = float(report.get("accuracy", 0.0))
            fmt = float(report.get("formatFidelity", 0.0))
            print(f"   产出：{report.get('message')}  文件 {report.get('outputSize', 0)} 字节")
            print(f"   工作表：{report.get('sheets')}")
            print(
                f"   值准确率：{acc * 100:.1f}%  ({report.get('matched')}/{report.get('total')} 格)"
                f"   格式保真：{fmt * 100:.1f}%"
                f"   期望 {report.get('expectedShape')} → 实际 {report.get('actualShape')}"
            )
            for item in report.get("missing", []):
                print(f"     · 缺 {item}")
            for item in report.get("wrong", []):
                print(f"     · 错 {item}")
            for item in report.get("formatLoss", []):
                print(f"     · 格式 {item}")
            if report.get("warnings"):
                print(f"     ⚠ {report['warnings']}")
            if args.verbose:
                for line in report.get("actual", []):
                    print(f"     | {line}")
        reports.append(report)

    scored = [r for r in reports if "accuracy" in r and "error" not in r]
    if scored:
        avg = sum(float(r["accuracy"]) for r in scored) / len(scored)
        avg_fmt = sum(float(r.get("formatFidelity", 0.0)) for r in scored) / len(scored)
        print("\n" + "=" * 78)
        print(
            f"平均值准确率：{avg * 100:.1f}%   最低：{min(float(r['accuracy']) for r in scored) * 100:.1f}%"
            f"   平均格式保真：{avg_fmt * 100:.1f}%"
        )
        by_case = {r["case"]: f"{float(r['accuracy']) * 100:.0f}%" for r in scored}
        print(f"分项：{by_case}")
        print("=" * 78)

    (WORK / "report.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n完整报告：{WORK / 'report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
