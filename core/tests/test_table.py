"""表格几何重建的测试。

本地离线引擎只吐文字行 + 坐标，表格结构靠这里的算法重建。
因此这些用例直接模拟"已识别出的文字块"，验证重建结果是否符合预期。
"""

from __future__ import annotations

from docforge.ocr.base import TextBlock
from docforge.ocr.table import blocks_to_grid, detect_columns, group_rows, guess_header_row


def _cell(text: str, x0: int, y0: int, x1: int, y1: int) -> TextBlock:
    return TextBlock(text=text, box=(x0, y0, x1, y1), confidence=0.95)


def _simple_table() -> list[TextBlock]:
    """3 列 × 3 行，列间隙清晰。"""
    blocks: list[TextBlock] = []
    headers = ["姓名", "部门", "金额"]
    data = [
        ["张三", "技术部", "1200"],
        ["李四", "市场部", "980"],
    ]

    for col, text in enumerate(headers):
        blocks.append(_cell(text, col * 200 + 10, 10, col * 200 + 90, 40))
    for row, values in enumerate(data, start=1):
        for col, text in enumerate(values):
            blocks.append(_cell(text, col * 200 + 10, row * 50 + 10, col * 200 + 90, row * 50 + 40))
    return blocks


# --------------------------------------------------------------------------- #
# 聚行                                                                          #
# --------------------------------------------------------------------------- #

def test_group_rows_clusters_by_vertical_overlap() -> None:
    blocks = [
        _cell("A1", 0, 0, 50, 20),
        _cell("B1", 100, 2, 150, 22),   # 与 A1 纵向重叠很多 → 同一行
        _cell("A2", 0, 60, 50, 80),     # 换行
        _cell("B2", 100, 62, 150, 82),
    ]
    rows = group_rows(blocks)
    assert len(rows) == 2
    assert [b.text for b in rows[0]] == ["A1", "B1"]
    assert [b.text for b in rows[1]] == ["A2", "B2"]


def test_group_rows_orders_within_row_by_x() -> None:
    blocks = [
        _cell("右", 300, 0, 350, 20),
        _cell("左", 0, 0, 50, 20),
        _cell("中", 150, 0, 200, 20),
    ]
    rows = group_rows(blocks)
    assert [b.text for b in rows[0]] == ["左", "中", "右"]


def test_group_rows_empty() -> None:
    assert group_rows([]) == []


# --------------------------------------------------------------------------- #
# 找列                                                                          #
# --------------------------------------------------------------------------- #

def test_detect_columns_finds_gaps() -> None:
    blocks = _simple_table()
    columns = detect_columns(blocks)
    # 三列内容分别在 x≈10-90 / 210-290 / 410-490 附近
    assert len(columns) == 3


def test_detect_columns_single_column_when_contiguous() -> None:
    blocks = [_cell("一整行文字", 0, 0, 500, 20), _cell("另一行", 0, 30, 400, 50)]
    columns = detect_columns(blocks)
    assert len(columns) == 1


def test_detect_columns_ignores_tiny_gaps() -> None:
    """相邻字之间的微小空隙不该被当成列分隔。"""
    blocks = [
        _cell("甲", 0, 0, 40, 20),
        _cell("乙", 42, 0, 80, 20),   # 只差 2px
        _cell("丙", 84, 0, 120, 20),
    ]
    assert len(detect_columns(blocks)) == 1


# --------------------------------------------------------------------------- #
# 完整重建                                                                      #
# --------------------------------------------------------------------------- #

def test_blocks_to_grid_builds_matrix() -> None:
    grid = blocks_to_grid(_simple_table())
    assert grid.rows == [
        ["姓名", "部门", "金额"],
        ["张三", "技术部", "1200"],
        ["李四", "市场部", "980"],
    ]
    assert grid.row_count == 3
    assert grid.column_count == 3


def test_blocks_to_grid_handles_missing_cells() -> None:
    """缺单元格时必须补空字符串，否则列会错位。"""
    blocks = [
        _cell("A", 0, 0, 50, 20),
        _cell("B", 200, 0, 250, 20),
        _cell("C", 400, 0, 450, 20),
        # 第二行只有第 1 列和第 3 列
        _cell("D", 0, 50, 50, 70),
        _cell("E", 400, 50, 450, 70),
    ]
    grid = blocks_to_grid(blocks)
    assert grid.rows[1] == ["D", "", "E"]


def test_blocks_to_grid_merges_multiple_fragments_in_one_cell() -> None:
    """同一格里的多段文字要合并，而不是各占一列。"""
    blocks = [
        _cell("合计", 0, 0, 60, 20),
        _cell("金额", 65, 0, 120, 20),   # 间隙很小，属于同一格
        _cell("1000", 300, 0, 380, 20),
    ]
    grid = blocks_to_grid(blocks)
    assert grid.rows[0][0] == "合计 金额"


def test_blocks_to_grid_trims_empty_rows_and_columns() -> None:
    blocks = [
        _cell("A", 0, 0, 50, 20),
        _cell("B", 200, 0, 250, 20),
        _cell("C", 0, 50, 50, 70),
    ]
    grid = blocks_to_grid(blocks)
    assert all(any(cell.strip() for cell in row) for row in grid.rows)
    assert all(any(row[c].strip() for row in grid.rows) for c in range(grid.column_count))


def test_blocks_to_grid_empty_input() -> None:
    grid = blocks_to_grid([])
    assert grid.row_count == 0
    assert grid.column_count == 0


def test_blocks_to_grid_single_column_text_dump() -> None:
    """没有表格结构的纯文本也应产出可用的单列结果，而不是报错。"""
    blocks = [_cell("第一行文字", 0, 0, 300, 20), _cell("第二行文字", 0, 40, 300, 60)]
    grid = blocks_to_grid(blocks)
    assert grid.column_count == 1
    assert [r[0] for r in grid.rows] == ["第一行文字", "第二行文字"]


# --------------------------------------------------------------------------- #
# 表头猜测                                                                      #
# --------------------------------------------------------------------------- #

def test_guess_header_row_finds_text_header() -> None:
    grid = [
        ["姓名", "部门", "金额"],
        ["张三", "技术部", "1200"],
    ]
    assert guess_header_row(grid) == 0


def test_guess_header_row_skips_numeric_first_row() -> None:
    """首行就是数据（含数字）时，不该被误判为表头。"""
    grid = [
        ["1", "2", "3"],
        ["姓名", "部门", "金额"],
        ["张三", "技术部", "1200"],
    ]
    assert guess_header_row(grid) == 1


def test_guess_header_row_empty() -> None:
    assert guess_header_row([]) == -1
