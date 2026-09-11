"""从文字框坐标重建表格结构。

## 为什么需要这一层

云端 DeepSeek Vision 可以直接按提示词返回结构化 JSON 表格。但**本地离线引擎
只吐文字行 + 坐标**，没有表格概念。如果只有云端能转 Excel，那"离线可用"这条
承诺就断了一半 —— 用户在没有密钥或内网环境下就做不了表格提取。

所以这里用几何信息把表格重建出来：

1. **聚行**：把纵向重叠的文字行归到同一行（表格里同一行的单元格 y 区间必然重叠）
2. **找列**：统计所有单元格的 x 覆盖情况，**覆盖率为 0 的连续区段就是列间隙**，
   间隙之间的区段即为一列。这比"按 x 中心聚类"稳健得多 ——
   同一列里短文本和长文本的中心可以差很远，但只要没有别的单元格插进来，
   覆盖法就不会把它们判成两列。
3. **填充**：把每个单元格按最大重叠投到对应列，空位补空字符串

这套方法对"有清晰列间隙"的表格（绝大多数办公表格）效果好，
对没有明显间隙的宽表会退化，此时结果仍是可用的（只是列数偏少）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import TextBlock

#: 判定"同一行"时，纵向重叠比例的门槛
ROW_OVERLAP_RATIO = 0.35
#: 列间隙的最小宽度（像素）。太小的间隙多半只是字间距，不是列分隔。
MIN_GAP_PX = 6
#: 单元格投列时要求的最小横向重叠比例
MIN_COLUMN_OVERLAP = 0.15


@dataclass
class TableGrid:
    """从坐标重建出的表格。"""

    rows: list[list[str]]
    #: 每一行的原始文字块（便于调试与回退）
    source_blocks: list[list[TextBlock]]

    @property
    def column_count(self) -> int:
        return max((len(r) for r in self.rows), default=0)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def to_dict(self) -> dict[str, object]:
        return {"rows": self.rows, "rowCount": self.row_count, "columnCount": self.column_count}


def _vertical_overlap(a: TextBlock, b: TextBlock) -> float:
    """两个块在纵向的重叠比例（相对较矮的那个）。"""
    ay0, ay1 = a.box[1], a.box[3]
    by0, by1 = b.box[1], b.box[3]
    top, bottom = max(ay0, by0), min(ay1, by1)
    if bottom <= top:
        return 0.0
    shorter = max(1, min(ay1 - ay0, by1 - by0))
    return (bottom - top) / shorter


def group_rows(blocks: list[TextBlock], tolerance: float = ROW_OVERLAP_RATIO) -> list[list[TextBlock]]:
    """把文字块聚成"行"。返回的每一行按 x 从左到右排序。"""
    if not blocks:
        return []

    ordered = sorted(blocks, key=lambda b: (b.box[1], b.box[0]))
    rows: list[list[TextBlock]] = []

    for block in ordered:
        placed = False
        for row in rows:
            # 与行内任意一块有足够纵向重叠，就认为属于同一行
            if any(_vertical_overlap(block, member) >= tolerance for member in row):
                row.append(block)
                placed = True
                break
        if not placed:
            rows.append([block])

    for row in rows:
        row.sort(key=lambda b: b.box[0])

    # 行按纵向位置排序（聚行过程中顺序可能被打乱）
    rows.sort(key=lambda row: min(b.box[1] for b in row))
    return rows


def detect_columns(blocks: list[TextBlock], *, min_gap: int = MIN_GAP_PX) -> list[tuple[int, int]]:
    """用"覆盖法"找出列区间。

    把每个文字块的横向区间投到一个坐标轴上，统计每个位置被覆盖的次数；
    覆盖率长期为 0 的区段就是列之间的间隙。
    """
    if not blocks:
        return []

    intervals = sorted((b.box[0], b.box[2]) for b in blocks if b.box[2] > b.box[0])
    if not intervals:
        return []

    x_min = intervals[0][0]
    x_max = max(right for _, right in intervals)
    if x_max <= x_min:
        return [(x_min, x_max)]

    # 用差分数组统计覆盖数，避免逐像素扫描大图
    events: list[tuple[int, int]] = []
    for left, right in intervals:
        events.append((left, 1))
        events.append((right, -1))
    events.sort()

    gaps: list[tuple[int, int]] = []
    coverage = 0
    gap_start: int | None = None

    for position, delta in events:
        coverage += delta

        if coverage == 0:
            # 刚离开所有区间 → 从当前位置起可能是列间隙
            gap_start = position
        elif gap_start is not None:
            # 又进入了一个区间 → 上一段间隙在此闭合
            if position - gap_start >= min_gap:
                gaps.append((gap_start, position))
            gap_start = None

    columns: list[tuple[int, int]] = []
    cursor = x_min
    for gap_start, gap_end in gaps:
        if gap_start > cursor:
            columns.append((cursor, gap_start))
        cursor = max(cursor, gap_end)
    if cursor < x_max:
        columns.append((cursor, x_max))

    return columns or [(x_min, x_max)]


def _assign_column(block: TextBlock, columns: list[tuple[int, int]]) -> int:
    """把单元格投到横向重叠最大的那一列。"""
    best_index = 0
    best_overlap = 0.0
    width = max(1, block.box[2] - block.box[0])

    for index, (left, right) in enumerate(columns):
        overlap = min(block.box[2], right) - max(block.box[0], left)
        if overlap <= 0:
            continue
        ratio = overlap / width
        if ratio > best_overlap:
            best_overlap = ratio
            best_index = index

    # 完全没有重叠（例如单元格跨越了整个表宽）：投到起始位置最近的列
    if best_overlap < MIN_COLUMN_OVERLAP:
        center = (block.box[0] + block.box[2]) / 2
        best_index = min(
            range(len(columns)),
            key=lambda i: abs((columns[i][0] + columns[i][1]) / 2 - center),
        )
    return best_index


def blocks_to_grid(blocks: list[TextBlock]) -> TableGrid:
    """把文字块重建为二维表格。"""
    rows = group_rows(blocks)
    if not rows:
        return TableGrid(rows=[], source_blocks=[])

    flat = [block for row in rows for block in row]
    columns = detect_columns(flat)
    width = len(columns)

    matrix: list[list[str]] = []
    sources: list[list[TextBlock]] = []

    for row in rows:
        cells: list[list[TextBlock]] = [[] for _ in range(width)]
        for block in row:
            cells[_assign_column(block, columns)].append(block)

        matrix.append([" ".join(b.text for b in cell).strip() for cell in cells])
        sources.append([cell[0] if cell else TextBlock("", (0, 0, 0, 0)) for cell in cells])

    matrix, sources = _trim_empty(matrix, sources)
    return TableGrid(rows=matrix, source_blocks=sources)


def _trim_empty(
    matrix: list[list[str]], sources: list[list[TextBlock]]
) -> tuple[list[list[str]], list[list[TextBlock]]]:
    """去掉全空的行与列，让结果更干净。"""
    if not matrix:
        return matrix, sources

    keep_rows = [i for i, row in enumerate(matrix) if any(cell.strip() for cell in row)]
    if not keep_rows:
        return [], []

    column_count = max(len(row) for row in matrix)
    keep_cols = [
        c for c in range(column_count) if any(row[c].strip() for row in matrix if c < len(row))
    ]

    trimmed = [[matrix[r][c] if c < len(matrix[r]) else "" for c in keep_cols] for r in keep_rows]
    trimmed_sources = [
        [sources[r][c] if c < len(sources[r]) else TextBlock("", (0, 0, 0, 0)) for c in keep_cols]
        for r in keep_rows
    ]
    return trimmed, trimmed_sources


def guess_header_row(grid: list[list[str]]) -> int:
    """猜测表头所在行。

    启发式：前几行中"没有数字、没有货币符号、单元格都较短"的那一行最可能是表头。
    猜错也无妨 —— 用户可以在界面上一键切换。
    """
    if not grid:
        return -1

    for index, row in enumerate(grid[:5]):
        cells = [c for c in row if c.strip()]
        if not cells:
            continue
        has_digit = any(any(ch.isdigit() for ch in cell) for cell in cells)
        all_short = all(len(cell) <= 12 for cell in cells)
        if not has_digit and all_short:
            return index
    return 0 if grid else -1
