"""图片转 Excel / 云端表格识别的回归测试。

## 这些用例保护的是什么

这一轮用真实 DeepSeek API 评测时，准确率从 **65% 提到 97%+**。
提升不是靠改提示词，而是修掉了三个"看不见"的缺陷。它们有个共同特征：
**先前的测试全都覆盖不到**，所以能长期潜伏。

1. **本地行高检测永远返回 0 个框**。RapidOCR 在纯检测模式下返回的是
   裸多边形 ``[box, box, ...]``，而代码按 ``(box, text, score)`` 解析，
   解析出的"文本"是坐标点，随后被当成非法数据丢掉。行高是切片决策的
   **唯一**输入 —— 它一直是 0，等于整套自适应切片都在用猜的数字做决定。

2. **小图被判成"字太小"而被竖着切开**。回退公式给出一张 784×354 的图
   "字号 6px"，于是切成两半：表格从中间劈开，右半边整列丢失，
   一张表还变成两张工作表。

3. **多切片返回的表格片段被当成多张表**。每块各返回一张"自己的表"，
   表头相同、行不同、重叠带还重复，直接交给下游就是好几张同名工作表。

另外还锁住两类"结果不理想"的表现：数值格式丢失（``1,250`` → ``1250``、
货币列退化成文本无法求和），以及引擎的提醒被静默丢弃。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from docforge.actions.base import summarize_with_warnings
from docforge.imaging import content_bbox
from docforge.ocr.local import _box_from_detect_item, _looks_like_point
from docforge.ocr.table import merge_table_fragments
from docforge.ocr.tiling import (
    GLYPH_FROM_BOX_RATIO,
    estimate_line_height,
    model_scale,
    plan_tiles,
    sanity_line_height,
)

FONT_PATH = "C:/Windows/Fonts/msyh.ttc"


# =========================================================================== #
# 1. 检测结果结构兼容（此前的 bug：永远解析出 0 个框）                          #
# =========================================================================== #

def test_looks_like_point() -> None:
    assert _looks_like_point([12, 34])
    assert _looks_like_point((12.5, 34.5))
    assert not _looks_like_point([[1, 2], [3, 4]])
    assert not _looks_like_point([])


def test_detect_item_accepts_bare_polygon() -> None:
    """``use_rec=False`` 下 RapidOCR 返回的就是一个裸多边形。

    这是那个 bug 的确切形态：多边形是 ``[[x,y], [x,y], [x,y], [x,y]]``，
    旧代码取 ``item[0]`` 得到的是**第一个点**而不是整个框。
    """
    polygon = [[69.0, 544.0], [83.0, 544.0], [83.0, 579.0], [69.0, 579.0]]
    assert _box_from_detect_item(polygon) == polygon


def test_detect_item_accepts_triple_form() -> None:
    """``use_rec=True`` 下是 ``(box, text, score)``，要取第 0 项。"""
    polygon = [[0.0, 0.0], [10.0, 0.0], [10.0, 8.0], [0.0, 8.0]]
    assert _box_from_detect_item([polygon, "文字", 0.97]) == polygon


@pytest.mark.parametrize("item", [None, [], [1], ["x", "y"]])
def test_detect_item_tolerates_garbage(item: object) -> None:
    """结构不认识时返回 None（调用方跳过），而不是抛异常打断整张图的识别。"""
    assert _box_from_detect_item(item) is None


@pytest.mark.skipif(not Path(FONT_PATH).is_file(), reason="需要中文字体")
def test_detect_lines_actually_returns_boxes(tmp_path: Path) -> None:
    """端到端回归：一张有文字的图必须能检出框。

    这条用例如果失败，说明"行高测量"整条链路又断了 ——
    而它断了不会报错，只会让切片决策退回瞎猜。
    """
    rapid = pytest.importorskip("rapidocr_onnxruntime")
    assert rapid is not None

    from docforge.ocr.local import get_engine

    engine = get_engine()
    available, reason = engine.availability()
    if not available:
        pytest.skip(f"本地 OCR 不可用：{reason}")

    image = Image.new("RGB", (900, 600), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(FONT_PATH, 34)
    for index in range(8):
        draw.text((60, 50 + index * 60), f"第 {index + 1} 行测试文字 ABCDEFG", font=font, fill="black")
    path = tmp_path / "lines.png"
    image.save(path)

    blocks = engine.detect_lines(path)
    assert len(blocks) >= 3, f"应当检出多行文字，实际 {len(blocks)} 个框"

    height = estimate_line_height(blocks)
    assert 20 < height < 90, f"行高估计应接近 34px 左右，实际 {height}"


# =========================================================================== #
# 2. 行高与切片决策                                                             #
# =========================================================================== #

def _block(text: str, height: int, top: int = 0):
    from docforge.ocr.base import TextBlock

    return TextBlock(text=text, box=(0, top, 100, top + height))


def test_estimate_line_height_uses_median_when_distribution_is_tight() -> None:
    """正文文档的框就是单行，分布很紧 —— 中位数最稳。"""
    blocks = [_block("正文一行", 40 + (i % 3)) for i in range(20)]
    assert 40 <= estimate_line_height(blocks) <= 42


def test_estimate_line_height_uses_low_percentile_when_boxes_merge_rows() -> None:
    """表格行距紧，检测框常把两三行并成一个 —— 此时中位数会高估一倍。

    实测数据：26 行表格的框高分布为 最小34 / p10 42 / 中位 94，
    而真实行距是 42。用中位数会把字号估成 2.2 倍，
    直接导致"该切片的图不切"，整图缩小后小字全糊。
    """
    heights = [34] * 10 + [42] * 20 + [94] * 40 + [118] * 20 + [172] * 10
    blocks = [_block("表格", h, top=i * 10) for i, h in enumerate(heights)]
    estimated = estimate_line_height(blocks)
    assert estimated < 60, f"应当识别出跨行框并用低分位，实际 {estimated}"


def test_estimate_line_height_ignores_empty_blocks() -> None:
    assert estimate_line_height([]) == 0.0
    assert estimate_line_height([_block("   ", 40)]) == 0.0


@pytest.mark.parametrize(
    ("raw", "height", "expected_low_ratio", "expected_high_ratio"),
    [
        (0.0, 1000, 0.004, 0.2),
        (3.0, 1000, 0.004, 0.2),      # 小到不可信 → 抬到下限
        (500.0, 1000, 0.004, 0.2),    # 大到不可信 → 压到上限
    ],
)
def test_sanity_line_height_clamps(raw: float, height: int, expected_low_ratio: float, expected_high_ratio: float) -> None:
    value = sanity_line_height(raw, height)
    assert height * expected_low_ratio - 1 <= value <= height * expected_high_ratio + 1


def test_no_tiling_when_model_would_not_downscale() -> None:
    """**核心回归**：模型不会缩小这张图时，绝不能切片。

    图片在 1300 等效像素以内 → 缩放比例 1.0 → 缩不损失任何可读性，
    切片只会把版面/表格切断。旧代码漏了这条判断，把一张 784×354
    的干净表格判成"字号 6px"并竖着切成两半，右半边整列丢失。
    """
    assert model_scale(784, 354) == 1.0
    tiles = plan_tiles(784, 354, 6.0)          # 故意给一个荒谬的小字号
    assert len(tiles) == 1, "不该切片"
    assert tiles[0].box == (0, 0, 784, 354)


def test_tiling_still_happens_for_genuinely_small_text() -> None:
    """反向确认：确实需要切的时候还得切（别把守卫写成了"永不切片"）。"""
    tiles = plan_tiles(2600, 3400, 20.0)
    assert len(tiles) > 1


def test_rows_layout_never_splits_columns() -> None:
    """表格只做横向切分：竖着切会把列结构劈碎，实测导致整列丢失。"""
    tiles = plan_tiles(3000, 4000, 20.0, layout="rows")
    assert len(tiles) > 1
    for tile in tiles:
        assert tile.x == 0 and tile.x + tile.width == 3000, "每一块都必须保留完整宽度"


def test_grid_layout_still_splits_columns() -> None:
    """纯文字内容仍可双向切 —— 版面对它没有"列"的概念。"""
    tiles = plan_tiles(3000, 4000, 20.0, layout="grid")
    assert any(tile.width < 3000 for tile in tiles)


def test_mode_off_forces_single_tile() -> None:
    assert len(plan_tiles(4000, 4000, 10.0, mode="off")) == 1


# =========================================================================== #
# 3. 表格片段的跨切片合并                                                       #
# =========================================================================== #

def _table(headers: list[str], rows: list[list[str]], title: str = "") -> dict:
    return {"title": title, "headers": headers, "rows": rows, "spans": []}


def test_merge_fragments_concatenates_same_table() -> None:
    fragment_a = _table(["部门", "金额"], [["技术部", "1,250"], ["市场部", "980"]])
    fragment_b = _table(["部门", "金额"], [["财务部", "640"], ["人事部", "420"]])

    merged = merge_table_fragments([fragment_a, fragment_b])
    assert len(merged) == 1
    assert merged[0]["rows"] == [
        ["技术部", "1,250"],
        ["市场部", "980"],
        ["财务部", "640"],
        ["人事部", "420"],
    ]


def test_merge_fragments_drops_the_overlap_seam() -> None:
    """相邻切片有重叠带，边界那一行会被两块都识别到。"""
    fragment_a = _table(["姓名", "部门"], [["张伟", "技术部"], ["李娜", "市场部"]])
    fragment_b = _table(["姓名", "部门"], [["李娜", "市场部"], ["王强", "财务部"]])

    merged = merge_table_fragments([fragment_a, fragment_b])
    assert merged[0]["rows"] == [
        ["张伟", "技术部"],
        ["李娜", "市场部"],
        ["王强", "财务部"],
    ], "接缝处的重复行应被去掉，且只去掉一份"


def test_merge_fragments_keeps_genuinely_different_tables_apart() -> None:
    """表头不同 = 真的是两张表，不能拼在一起。"""
    first = _table(["部门", "金额"], [["技术部", "1,250"]])
    second = _table(["产品", "库存"], [["复印纸", "1,240"]])

    merged = merge_table_fragments([first, second])
    assert len(merged) == 2


def test_merge_fragments_does_not_dedupe_globally() -> None:
    """同片段内重复的数据行必须原样保留 —— 不能做全局去重。

    表格里本来就可能有两笔金额完全一样的记录，全局去重会把真实数据吃掉。
    这里用"片段内重复 + 下一片段有新行"来验证：只有拼接缝才去重。

    （如果两个片段的内容**完全一样**，那是无法区分的歧义情形：
    既可能是重叠带，也可能是真的重复。此时按重叠处理 —— 重叠是切片的常态。）
    """
    fragment_a = _table(["项目", "金额"], [["复印纸", "120"], ["复印纸", "120"]])
    fragment_b = _table(["项目", "金额"], [["签字笔", "35"]])

    merged = merge_table_fragments([fragment_a, fragment_b])
    rows = merged[0]["rows"]
    assert len(rows) == 3, f"片段内重复的两行都要保留，实际 {rows}"
    assert rows[:2] == [["复印纸", "120"], ["复印纸", "120"]]


def test_merge_fragments_treats_thousands_separator_as_significant() -> None:
    """`1,250` 与 `1250` 不视为同一行：千分位是模型漏写的典型表现。"""
    fragment_a = _table(["部门", "金额"], [["技术部", "1,250"]])
    fragment_b = _table(["部门", "金额"], [["技术部", "1250"]])

    merged = merge_table_fragments([fragment_a, fragment_b])
    assert len(merged[0]["rows"]) == 2


def test_merge_fragments_drops_blank_rows() -> None:
    fragment_a = _table(["A"], [["1"]])
    fragment_b = _table(["A"], [[""], ["2"]])
    merged = merge_table_fragments([fragment_a, fragment_b])
    assert merged[0]["rows"] == [["1"], ["2"]]


def test_merge_fragments_handles_empty_input() -> None:
    assert merge_table_fragments([]) == []


def test_merge_fragments_keeps_first_table_when_headers_empty() -> None:
    """没有表头时无法判断是不是同一张表 —— 保持分开，不做猜测性合并。"""
    first = _table([], [["1", "2"]])
    second = _table([], [["3", "4"]])
    assert len(merge_table_fragments([first, second])) == 2


# =========================================================================== #
# 4. 内容裁边                                                                   #
# =========================================================================== #

@pytest.mark.skipif(not Path(FONT_PATH).is_file(), reason="需要中文字体")
def test_content_bbox_finds_the_content_region() -> None:
    """四周大片留白要被识别出来 —— 这是"远距离拍一整页"能救回来的关键。"""
    canvas = Image.new("RGB", (1200, 1600), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype(FONT_PATH, 28)
    for index in range(6):
        draw.text((400, 600 + index * 40), f"正文第 {index + 1} 行", font=font, fill="black")

    box = content_bbox(canvas)
    assert box is not None
    x0, y0, x1, y1 = box
    assert 300 < x0 < 500
    assert 500 < y0 < 700
    assert (x1 - x0) < 700, "裁边后宽度应明显小于原图"
    assert (x1 - x0) * (y1 - y0) < 1200 * 1600 * 0.3


def test_content_bbox_detects_non_white_background() -> None:
    """背景不假定为纯白：手机拍纸页时纸张是灰的、桌面是深色的。"""
    canvas = Image.new("RGB", (1000, 1000), (70, 80, 90))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([300, 300, 700, 700], fill=(230, 228, 220))

    box = content_bbox(canvas)
    assert box is not None
    x0, y0, x1, y1 = box
    assert 260 < x0 < 340 and 260 < y0 < 340
    assert 660 < x1 < 740 and 660 < y1 < 740


def test_content_bbox_returns_none_when_there_is_nothing_to_trim() -> None:
    """整图都是内容时不该裁，否则白白多一次重编码。"""
    canvas = Image.new("RGB", (600, 600), "white")
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([2, 2, 597, 597], fill=(20, 20, 20))
    assert content_bbox(canvas) is None


def test_content_bbox_returns_none_for_blank_image() -> None:
    assert content_bbox(Image.new("RGB", (600, 400), "white")) is None


def test_content_bbox_returns_none_for_tiny_image() -> None:
    assert content_bbox(Image.new("RGB", (16, 16), "white")) is None


# =========================================================================== #
# 5. 数值与显示格式                                                             #
# =========================================================================== #

@pytest.mark.parametrize(
    ("raw", "expected_value", "expected_format"),
    [
        ("1,250", 1250, "#,##0"),
        ("1250", 1250, "0"),
        ("18.50", 18.5, "0.00"),
        ("¥128,600.00", 128600, '"¥"#,##0.00'),
        ("$1,234.56", 1234.56, '"$"#,##0.00'),
        ("12.5%", 0.125, "0.0%"),
        ("-3,200", -3200, "#,##0"),
        ("(1,234)", -1234, "#,##0;#,##0"),
        # 不转换：编号、枚举值、非数字
        ("007", "007", None),
        ("DF-1001", "DF-1001", None),
        ("甲,乙", "甲,乙", None),
        ("优秀", "优秀", None),
        ("", "", None),
        ("2026-03-15", "2026-03-15", None),
    ],
)
def test_parse_cell_value_and_format(raw: str, expected_value: object, expected_format: str | None) -> None:
    """值与显示格式一起给：既能参与计算，显示又与原件一致。

    早先只做"转数字"，结果是 ``1,250`` 显示成 ``1250``（千分位没了）、
    ``¥128,600.00`` 因为带货币符号转换失败干脆留成文本（那一列无法求和）。
    两种都是用户眼里的"结果不理想"。
    """
    from docforge.actions.image_table import _parse_cell

    value, number_format = _parse_cell(raw)
    assert value == expected_value
    assert number_format == expected_format


def test_currency_cells_are_numeric_not_text() -> None:
    """回归：货币列必须是数字，否则用户在 Excel 里求和会得到 0。"""
    from docforge.actions.image_table import _parse_cell

    value, _ = _parse_cell("¥128,600.00")
    assert isinstance(value, (int, float))


def test_glyph_ratio_is_sane() -> None:
    """字形高度换算系数必须落在合理区间。

    :func:`estimate_line_height` 已经用低分位挑出"单行框"，所以系数接近 1.0。
    定成 0.6 之类的值会**系统性低估**字号，把识别得好好的图也报成"字太小"
    （实测用户一张 1264×2800 的截图结果完全正确，界面上却挂着吓人的警告）。
    """
    assert 0.8 <= GLYPH_FROM_BOX_RATIO <= 1.2


# =========================================================================== #
# 6. 提醒必须传到用户眼前                                                       #
# =========================================================================== #

def test_summarize_with_warnings_appends_first_warning() -> None:
    """模型认不清时会**编**出看起来合理的内容，任务却是绿色的成功。

    所以引擎的提醒必须进摘要 —— 用户看到"成功"就不会再复查结果。
    """
    from docforge.actions.base import ActionContext

    ctx = ActionContext(job_id="j", task_id="t", file_path="a.png", output_path="b.xlsx", params={})
    summary = summarize_with_warnings(ctx, "1 张表 · 6 行", ["字号偏小，结果可能不准", "已裁掉空白"])
    assert summary.startswith("1 张表 · 6 行")
    assert "⚠" in summary
    assert "字号偏小" in summary
    assert "共 2 条" in summary


def test_summarize_with_warnings_logs_every_warning() -> None:
    from docforge.actions.base import ActionContext

    logged: list[str] = []
    ctx = ActionContext(
        job_id="j",
        task_id="t",
        file_path="a.png",
        output_path="b.xlsx",
        params={},
        _log=logged.append,
    )
    summarize_with_warnings(ctx, "摘要", ["第一条", "第二条"])
    assert len(logged) == 2
    assert all("提醒" in line for line in logged)


def test_summarize_with_warnings_is_a_noop_without_warnings() -> None:
    from docforge.actions.base import ActionContext

    ctx = ActionContext(job_id="j", task_id="t", file_path="a.png", output_path="b.xlsx", params={})
    assert summarize_with_warnings(ctx, "1 张表 · 6 行", []) == "1 张表 · 6 行"


# =========================================================================== #
# 7. 输出预算被"思考"吃光（用户实际踩到的那个缺陷）                              #
# =========================================================================== #

@pytest.fixture()
def fake_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """注入一个假的 API Key。

    测试环境把数据目录重定向到了仓库内的临时路径，里面没有密钥，
    ``_post`` 会在发请求之前就抛 OcrUnavailable。这里只替换取密钥这一步，
    请求本身由 MockTransport 拦截，不会真的联网。
    """
    monkeypatch.setattr("docforge.ocr.deepseek.get_secret", lambda name: "sk-test-key")


def _completion(content: str, *, finish: str, reasoning: int = 0) -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": 1500,
            "completion_tokens": reasoning + 10,
            "completion_tokens_details": {"reasoning_tokens": reasoning},
        },
    }


def _client_with(handler) -> object:
    import httpx

    return httpx.Client(base_url="https://example.invalid", transport=httpx.MockTransport(handler))


def test_truncated_response_retries_with_a_bigger_budget(fake_api_key: None) -> None:
    """**核心回归**：``finish_reason == "length"`` 时必须提高预算重试。

    ``deepseek-flash`` 是推理模型，思考 token 也算在 ``max_tokens`` 里。
    实测表格模式下一次思考就要 7000~11000 tokens —— 原来的 8192 预算被思考
    吃光，``content`` 返回**空字符串**，用户看到的是"任务成功、一个字都没识别出来"。
    """
    import json as _json

    import httpx

    from docforge.ocr.deepseek import DeepSeekVisionEngine

    engine = DeepSeekVisionEngine()
    seen_budgets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_budgets.append(_json.loads(request.content)["max_tokens"])
        if len(seen_budgets) == 1:
            # 第一次：思考把预算吃光，内容为空
            return httpx.Response(200, json=_completion("", finish="length", reasoning=8192))
        return httpx.Response(200, json=_completion('{"tables":[],"text":"好"}', finish="stop"))

    content, _usage = engine._post(
        b"x", "image/png", "prompt",
        json_mode=True, detail="original", max_tokens=8192,
        client=_client_with(handler),
    )

    assert content, "重试后应当拿到内容"
    assert seen_budgets[0] == 8192
    assert seen_budgets[1] == 16384, "第二次必须把预算翻倍，否则重试没有意义"


def test_persistent_truncation_raises_instead_of_returning_empty(fake_api_key: None) -> None:
    """一直截断时必须**报错**，不能把空内容当成正常返回。

    静默返回空 → 任务显示"成功"、Excel 是空的，用户根本不会去重拍或换模式。
    """
    import json as _json

    import httpx

    from docforge.ocr.base import OcrError
    from docforge.ocr.deepseek import ABSOLUTE_MAX_TOKENS, DeepSeekVisionEngine

    engine = DeepSeekVisionEngine()
    budgets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        budgets.append(_json.loads(request.content)["max_tokens"])
        return httpx.Response(200, json=_completion("", finish="length", reasoning=8192))

    with pytest.raises(OcrError, match="截断"):
        engine._post(
            b"x", "image/png", "prompt",
            json_mode=True, detail="original", max_tokens=8192,
            client=_client_with(handler),
        )

    assert budgets[-1] <= ABSOLUTE_MAX_TOKENS, "预算不应无限增长"


def test_default_output_budget_leaves_room_for_reasoning() -> None:
    """默认预算必须给"思考"留出余量。

    实测：纯文字模式思考仅 240 tokens，表格模式要 7284~10885。
    默认值若停在 8192，表格模式必然被截断。
    """
    from docforge.ocr.base import OcrOptions

    assert OcrOptions().max_tokens >= 16384


def test_absolute_budget_cap_is_generous() -> None:
    """上限是天花板而不是花费（模型答完就停），所以尽管给足。"""
    from docforge.ocr.deepseek import ABSOLUTE_MAX_TOKENS

    assert ABSOLUTE_MAX_TOKENS >= 32768
