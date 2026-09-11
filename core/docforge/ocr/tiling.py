"""自适应切片与跨切片结果合并。

## 为什么必须切片

DeepSeek Vision 在推理前会把图片缩放到"约 1300×1300 等效像素"，且**单图上限 1024 token**。
对版面稀疏的图片这完全够用，但对密集小字文档是致命的：

    一张 4000×3000 的文档照片，被缩放 2.66 倍后，
    原本 12px 高的字只剩 4.5px —— 模型根本认不出来。

因此正确做法是**按语义需要把大图切成若干块分别识别**，再把结果按坐标拼回去。

## 切片尺寸怎么算（本模块的核心）

设图片中一个文字行的像素高度为 ``h``，模型对尺寸为 ``L×L`` 的切片会缩放
``s = min(1, MAX_SIDE/L)``，缩放后行高为 ``h·s``。要求它不低于可读阈值 ``MIN_LINE_PX``：

    h · (MAX_SIDE / L) ≥ MIN_LINE_PX   →   L ≤ MAX_SIDE · h / MIN_LINE_PX

于是切片边长上限由"字号"直接决定，而且结论很符合直觉：

* **300 DPI 的 A4 扫描件**（2480×3508，行高约 42px）
  → L ≤ 1300 × 42/12 ≈ 4550 > 3508，**整图一次识别即可，无需切片**
* **4000×3000 的密集文档照片**（行高约 12px）
  → L ≤ 1300 × 12/12 = 1300，需要切成约 4×3 块

也就是说：**不是"图大就切"，而是"字小才切"**。盲目按固定网格切图既慢又贵，
还会因为切断文本行而降低准确率。

## 合并时如何避免重复

相邻切片之间有重叠带，同一行字可能被两块都识别到。做法是给每块切片划出
"核心区"（不含重叠带），把每个文本块**归属给"它最靠近中心"的那块切片**，
从而天然去重；再做一次近位置同文本的去重兜底。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .base import OcrError, TextBlock

# 模型会把长边缩放到这个尺寸以内
MODEL_MAX_SIDE = 1300
# 模型缩放后的等效像素预算（1300 × 1300）
MODEL_BUDGET_PX = MODEL_MAX_SIDE * MODEL_MAX_SIDE
# 缩放后文字行高低于此值就容易误识。CJK 字形在 12px 以下辨识度急剧下降。
MIN_READABLE_LINE_PX = 12.0
#: 检测框高度换算成"字形高度"的经验系数。
#:
#: OCR 的行框包含行距，不等于字形本身的高度。实测两张表：字号 46px 的
#: 框高 88、字号 22px 的框高 42，比值都在 1.9 左右 —— 乘以 0.6 即得字形高度。
GLYPH_FROM_BOX_RATIO = 0.6
#: 触发"字号偏小、结果可能不准"提醒的门槛。
#:
#: 刻意比 MIN_READABLE_LINE_PX 严格：两边的代价不对称 ——
#: 误报只是让用户多看一句提示；漏报则是**静默交付编造的数据**
#: （模型认不清时会编出看起来合理的内容，任务却是绿色的"成功"）。
#: 因此宁可多提醒。实测 17px 时准确率 98%，11px 时掉到 83%，门槛落在两者之间。
MIN_RELIABLE_GLYPH_PX = 15.0
# 相邻切片的默认重叠比例（相对切片边长）
DEFAULT_OVERLAP = 0.12
# 切片数量上限：防止极小字号导致切成几百块（既慢又贵）
MAX_TILES = 64
# 行高未知时的保守估计：按图片高度的这个比例假定一行文字的高度
UNKNOWN_LINE_RATIO = 0.012
# 行高的合理区间（相对图片高度）。超出这个范围说明测量不可信。
MIN_LINE_RATIO = 0.004
MAX_LINE_RATIO = 0.2
# 判断"同一行"的纵向容差系数（相对中位行高）
ROW_TOLERANCE = 0.65


@dataclass(frozen=True)
class Tile:
    """一个待识别切片。

    ``x/y/width/height`` 是送进模型的裁剪区域；``core`` 是去掉重叠带后的核心区，
    用于把识别结果归属到唯一一块切片。
    """

    x: int
    y: int
    width: int
    height: int
    core: tuple[int, int, int, int]

    @property
    def box(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.width, self.y + self.height)

    def contains_in_core(self, px: float, py: float) -> bool:
        x0, y0, x1, y1 = self.core
        return x0 <= px < x1 and y0 <= py < y1

    def center_distance(self, px: float, py: float) -> float:
        """块中心到本切片中心的距离。越小说明越"居中"，越不容易被边缘裁切。"""
        cx = self.x + self.width / 2
        cy = self.y + self.height / 2
        return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


def model_scale(width: int, height: int) -> float:
    """模型对给定尺寸图片的缩放比例（<1 表示会被缩小）。"""
    if width <= 0 or height <= 0:
        return 1.0
    by_side = MODEL_MAX_SIDE / max(width, height)
    by_area = (MODEL_BUDGET_PX / (width * height)) ** 0.5
    return min(1.0, by_side, by_area)


def readable_line_height(line_height: float, width: int, height: int) -> float:
    """该行文字经模型缩放后的实际像素高度。"""
    return line_height * model_scale(width, height)


def max_tile_side(line_height: float) -> int:
    """由字号反推切片边长上限（见模块文档的推导）。"""
    if line_height <= 0:
        return MODEL_MAX_SIDE
    return max(64, int(MODEL_MAX_SIDE * line_height / MIN_READABLE_LINE_PX))


def sanity_line_height(line_height: float, height: int) -> float:
    """把行高夹到合理区间；明显不可信时按图片高度重新估一个。

    行高是切片决策的**唯一**输入，所以它一旦失真，后果是灾难性的。
    实测踩过：本地 OCR 在某张干净表格图上返回 0 个文本框 →
    ``estimate_line_height`` 得 0 → 回退公式算出 4.25px →
    被 ``max(6.0, ...)`` 抬到 6px → 判定"字号极小"，把一张 784×354 的图
    **竖着切成两半**。表格被从中间劈开，模型每半张只看到 3 列，
    于是"四季度""全年合计"两列整列丢失，同一张表还变成了两张工作表。

    这里做两层保护：
      * 相对图片高度夹到 [MIN_LINE_RATIO, MAX_LINE_RATIO]；
      * 完全测不到（<=0）时用回退比例，但同样受上面的夹取约束。
    """
    if height <= 0:
        return max(1.0, line_height)
    low = max(2.0, height * MIN_LINE_RATIO)
    high = max(low, height * MAX_LINE_RATIO)
    value = line_height if line_height > 0 else height * UNKNOWN_LINE_RATIO
    return min(high, max(low, value))


def plan_tiles(
    width: int,
    height: int,
    line_height: float = 0.0,
    *,
    mode: str = "auto",
    overlap: float = DEFAULT_OVERLAP,
    layout: str = "grid",
) -> list[Tile]:
    """规划切片。

    :param line_height: 图片中一个文字行的像素高度。传 0 表示未知，会按图片高度估算。
    :param mode: ``off`` 强制整图；``always`` 强制按尺寸上限切；``auto`` 按字号判断。
    :param layout: ``grid`` 双向切；``rows`` **只做横向切分**（保持整幅宽度）。

        ``rows`` 是给表格这类"结构由列定义"的图片用的。表格一旦被竖着切开，
        每一块都只剩部分列、而且表头对不上，合并时几乎无法还原 ——
        实测就是整列整列地丢。只做横向切分时，每块都是完整的列结构，
        合并只需要把行拼起来（见 :func:`docforge.ocr.table.merge_table_fragments`）。
    """
    if width <= 0 or height <= 0:
        raise OcrError("图片尺寸非法")

    whole = [Tile(0, 0, width, height, (0, 0, width, height))]

    if mode == "off":
        return whole

    # 模型根本不会缩小这张图 —— 缩放不损失任何可读性，切片只会把版面/表格切断。
    # 这条判断必须放在字号之前：字号是"估"出来的，而缩放比例是**算**出来的，
    # 后者永远不会错。实测正是漏了这条才把一张 784×354 的小图切成两半。
    if mode != "always" and model_scale(width, height) >= 1.0:
        return whole

    line_height = sanity_line_height(line_height, height)

    side_limit = max_tile_side(line_height)

    # 字够大时整图即可，避免无谓的多次调用（省钱也更快）
    if mode != "always" and side_limit >= max(width, height):
        return whole

    if mode == "always":
        # 强制切片时不能只依赖字号推导的上限 —— 字大时那个上限可能超过整图尺寸，
        # 结果仍是 1×1，"强制"就形同虚设。这里再额外限制到至少 2×2。
        side_limit = min(side_limit, max(64, min(width, height) // 2))

    cols = max(1, -(-width // side_limit))
    rows = max(1, -(-height // side_limit))

    if layout == "rows":
        # 只做横向切分：宽度必须完整保留，否则表格的列结构就毁了
        cols = 1

    # 切片过多说明字号极小（例如整页缩略图）。此时切得再细也认不出来，
    # 不如限制数量并在上层给出提示，避免用户白等与白花钱。
    while cols * rows > MAX_TILES:
        if cols >= rows and cols > 1:
            cols -= 1
        elif rows > 1:
            rows -= 1
        else:
            break

    cell_w = width / cols
    cell_h = height / rows
    pad_x = int(cell_w * overlap)
    pad_y = int(cell_h * overlap)

    tiles: list[Tile] = []
    for row in range(rows):
        for col in range(cols):
            cx0 = int(round(col * cell_w))
            cy0 = int(round(row * cell_h))
            cx1 = int(round((col + 1) * cell_w)) if col < cols - 1 else width
            cy1 = int(round((row + 1) * cell_h)) if row < rows - 1 else height

            x0 = max(0, cx0 - pad_x)
            y0 = max(0, cy0 - pad_y)
            x1 = min(width, cx1 + pad_x)
            y1 = min(height, cy1 + pad_y)

            tiles.append(Tile(x0, y0, x1 - x0, y1 - y0, (cx0, cy0, cx1, cy1)))

    return tiles


def estimate_line_height(blocks: list[TextBlock]) -> float:
    """用本地 OCR 的检测框估计"一行文字占多高"，作为切片决策的输入。

    ## 为什么不能直接用中位数

    中位数在**正文文档**上很稳，但在**表格**上会明显偏大：表格行距紧，
    检测框常常把相邻两三行并成一个框。实测一张 26 行表格的框高分布：

        最小 34  p10 42  p25 70  中位 94  p75 118   实际行距 42

    中位数 94 是真实行距的 **2.2 倍**。这会一路传导到切片决策 ——
    字号被高估 2 倍，切片边长上限就大 2 倍，于是"本来该切"的图不切，
    整图被模型大幅缩小后小字全部糊掉（实测因此把整列数据读错）。

    所以这里做**自适应**：分布很紧说明框就是单行，用中位数最稳；
    分布很宽说明有框跨了多行，改用低分位数（更接近真实行距）。

    取向是"宁可低估"：低估只会多切几块（多花一点点钱、有上限保护），
    高估却会让小字彻底认不出来，而且模型认不出时**不会报错，会编**。
    """
    heights = sorted(b.line_height for b in blocks if b.text.strip() and b.line_height > 0)
    if not heights:
        return 0.0

    count = len(heights)
    median = float(heights[count // 2])
    p10 = float(heights[max(0, int(count * 0.10))])

    # 分布松散（中位数超过低分位的两倍）→ 判定存在跨行框，用低分位
    if median > p10 * 2:
        return p10
    return median


def merge_tile_blocks(
    tile_blocks: list[tuple[Tile, list[TextBlock]]],
    *,
    iou_threshold: float = 0.3,
    offset_ratio: float = 0.75,
) -> list[TextBlock]:
    """把各切片的识别结果按坐标合并成全局结果。

    相邻切片之间有重叠带，同一行字可能被两块都识别到，必须去重。做法：

      1. **坐标归一**：切片内坐标 → 原图全局坐标，同时记录它来自哪块切片。
      2. **归属判定**：优先归给"核心区包含其中心"的切片；若有多个或都没有，
         则选离该切片中心最近的那块。核心区是对全图的无缝划分，因此这一步天然
         让重叠带里的重复落到同一块切片上。
      3. **空间聚类去重**：把位置重合（IoU 高）或位移很小（相对行高）且文本
         互为包含关系的块聚成一簇，每簇只保留一条。

    第 3 步刻意同时接受"位置邻近"和"文本互相包含"两个条件：
    被切片边缘裁断的半行字，其文本往往是完整文本的前缀，靠 IoU 是合并不掉的。
    """
    entries: list[tuple[TextBlock, Tile]] = []

    for tile, blocks in tile_blocks:
        for block in blocks:
            gx0 = block.box[0] + tile.x
            gy0 = block.box[1] + tile.y
            gx1 = block.box[2] + tile.x
            gy1 = block.box[3] + tile.y
            entries.append(
                (TextBlock(text=block.text, box=(gx0, gy0, gx1, gy1), confidence=block.confidence), tile)
            )

    # 预先把每块的归属切片算出来（重复计算代价高，且逻辑集中好维护）
    owners: list[Tile] = []
    for block, _ in entries:
        cx, cy = block.center
        candidates = [t for t, _ in tile_blocks if t.contains_in_core(cx, cy)]
        if not candidates:
            candidates = [t for t, _ in tile_blocks]
        owners.append(min(candidates, key=lambda t: t.center_distance(cx, cy)))

    # 置信度高的先入簇，让它成为代表，减少被截断文本"抢占"簇的机会
    order = sorted(range(len(entries)), key=lambda i: -entries[i][0].confidence)

    clusters: list[list[int]] = []
    for index in order:
        block = entries[index][0]
        text = _normalize(block.text)
        if not text:
            continue

        target: list[int] | None = None
        for cluster in clusters:
            rep = entries[cluster[0]][0]
            rep_text = _normalize(rep.text)
            if not (text == rep_text or text in rep_text or rep_text in text):
                continue  # 文本不兼容，不可能是同一行的重复
            if _iou(block.box, rep.box) >= iou_threshold or _near(block.box, rep.box, offset_ratio):
                target = cluster
                break

        if target is None:
            clusters.append([index])
        else:
            target.append(index)

    result: list[TextBlock] = []
    for cluster in clusters:
        # 同一簇内取"信息最完整"的那条：文本最长优先，其次置信度最高。
        # 这样被裁断的半行不会覆盖完整的整行。
        best = max(
            (entries[i][0] for i in cluster),
            key=lambda b: (len(_normalize(b.text)), b.confidence),
        )
        result.append(best)

    return result


def _normalize(text: str) -> str:
    """归一化文本用于比较：去掉空白与常见标点差异。"""
    return "".join(ch for ch in text if not ch.isspace())


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = max(1, (ax1 - ax0) * (ay1 - ay0))
    area_b = max(1, (bx1 - bx0) * (by1 - by0))
    return inter / (area_a + area_b - inter)


def _near(
    a: tuple[int, int, int, int],
    b: tuple[int, int, int, int],
    offset_ratio: float = 0.75,
) -> bool:
    """两个框是否"基本在同一位置"（容许切片边界带来的轻微位移）。

    容差按行高比例给，而不是固定像素 —— 大字文档容忍的绝对位移本来就更大。
    """
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    min_height = min(ay1 - ay0, by1 - by0)
    tol = max(6, int(offset_ratio * min_height))
    return abs(ax0 - bx0) <= tol and abs(ay0 - by0) <= tol and abs(ax1 - bx1) <= tol and abs(ay1 - by1) <= tol


def sort_reading_order(blocks: list[TextBlock]) -> list[TextBlock]:
    """按阅读顺序排序：先按行分组（自上而下），行内再自左向右。

    直接用 ``(y, x)`` 排序在真实文档上效果很差 —— 同一行里几个字框的 y 只要有
    一两像素差异，顺序就会乱掉。因此需要先按"行"聚类。
    """
    if not blocks:
        return []

    heights = sorted(b.line_height for b in blocks)
    median_h = heights[len(heights) // 2] if heights else 12
    tolerance = max(4.0, median_h * ROW_TOLERANCE)

    ordered = sorted(blocks, key=lambda b: b.center[1])
    rows: list[list[TextBlock]] = []
    for block in ordered:
        placed = False
        for row in rows:
            row_center = sum(b.center[1] for b in row) / len(row)
            if abs(block.center[1] - row_center) <= tolerance:
                row.append(block)
                placed = True
                break
        if not placed:
            rows.append([block])

    result: list[TextBlock] = []
    for row in rows:
        result.extend(sorted(row, key=lambda b: b.center[0]))
    return result


def plan_tiles_for_image(image_path: Path | str, line_height: float = 0.0, mode: str = "auto") -> list[Tile]:
    """便捷入口：打开图片并按实际尺寸规划切片。"""
    from PIL import Image

    try:
        with Image.open(image_path) as image:
            width, height = image.size
    except OSError as err:
        raise OcrError(f"无法读取图片尺寸：{err}") from err
    return plan_tiles(width, height, line_height, mode=mode)
