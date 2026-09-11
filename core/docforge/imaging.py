"""图像能力引导。

## 为什么需要这个模块

Pillow 默认**读不了 HEIC/HEIF**（iPhone 的默认照片格式），需要额外注册
``pillow_heif`` 提供的打开器。而"注册"这个动作只有真正执行了才生效 ——
仅仅 `pip install pillow-heif` 是不够的。

这带来两个后果：

1. **功能承诺落空**：图片水印与 OCR 的 accepts 里都写着 ``heic``/``heif``，
   但用户拖进一张 iPhone 照片会直接报"无法打开图片"。
2. **打包后行为不一致**：PyInstaller 只收集被静态 import 的模块，
   没人 import 的 ``pillow_heif`` 不会进包 —— 开发环境里明明可用，
   装出来的程序却不行（实测就是这样）。

因此这里统一在**内核启动时**注册一次，问题一次性解决；
同时 spec 里把 ``pillow_heif`` 列进 hiddenimports，保证它真的被打进包里。
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_registered: bool | None = None


def ensure_heif_support() -> bool:
    """注册 HEIF/HEIC 打开器。返回是否可用。

    幂等且线程安全：内核启动时会调一次，动作里也可以放心再调。
    """
    global _registered

    with _lock:
        if _registered is not None:
            return _registered

        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
            _registered = True
        except Exception:  # noqa: BLE001 - 缺依赖或注册失败都只是少一种格式
            _registered = False

        return _registered


def heif_available() -> bool:
    """只查询可用性，不产生副作用（供引擎探测使用）。"""
    import importlib.util

    return importlib.util.find_spec("pillow_heif") is not None


# --------------------------------------------------------------------------- #
# 内容裁边                                                                      #
# --------------------------------------------------------------------------- #

def content_bbox(
    image: "object",
    *,
    threshold: int = 34,
    margin_ratio: float = 0.008,
) -> tuple[int, int, int, int] | None:
    """找出图片中"有内容"的外接矩形，用于裁掉四周空白。

    ## 为什么这一步对识别率影响巨大

    云端视觉模型会把图片缩到"约 1300×1300 等效像素"。**缩放比例是整图决定的**，
    所以四周的空白边距会直接吃掉正文的分辨率：

        一张 2400×3200 的照片里，表格只占中间 700×900，
        模型仍按 2400×3200 缩放（比例 0.41），22px 的字变成 9px —— 认不出来。
        把空白裁掉后是 700×900，**根本不触发缩放**，字原样送进模型。

    "远距离拍一整页"和"扫描件留白过多"这两种很常见的情形，收益都来自这一步。

    ## 为什么先做"去背景"

    直接拿像素和背景色比、或者"逐行取各自的中位数"，在**手机照片**上都会失效：
    光照不均让画面从左上到右下连续渐变，一行的左端比右端亮三四十个灰阶，
    于是整行都被判成"有内容" —— 边界框退化成整张图，等于没裁。

    这里改成先估出**低频照明**再减掉它：把图缩到很小的尺寸（文字的笔画在那一级
    已经被平均掉，只剩下照明与底色），再放大回来与原图相减。
    剩下的残差里只有"锐利的东西"——文字、表格线、印章，
    而平滑的光照渐变被减掉了。这是文档图像处理的常规做法，
    代价只有两次缩放。

    返回 ``None`` 表示不值得裁（几乎没有留白，或判断不可信）。
    """
    import numpy as np
    from PIL import Image

    width, height = image.size
    if width < 32 or height < 32:
        return None

    gray_image = image.convert("L")
    gray = np.asarray(gray_image, dtype=np.int16)

    # 低频照明估计：缩到长边约 96px 再放回来。
    # 缩放用双线性，等价于一次大半径的模糊，但比高斯卷积快得多。
    target = 96
    factor = max(2, max(width, height) // target)
    small = gray_image.resize(
        (max(1, width // factor), max(1, height // factor)), Image.Resampling.BILINEAR
    )
    background = np.asarray(small.resize((width, height), Image.Resampling.BILINEAR), dtype=np.int16)

    deviation = np.abs(gray - background)

    # 判定"这一行 / 这一列有没有内容"时，要求**足够多的像素**同时偏离，
    # 而不是"有一个像素偏离就算"。手机照片的传感器噪点总有几个像素偏差较大，
    # 只看最大值的话，一整行空白也会被噪点判成内容，边界框就退化成整张图了
    # （实测 3000×4000 的照片因此只裁掉 4%，等于没裁）。
    hits = deviation > threshold
    min_row_hits = max(3, int(width * 0.003))
    min_col_hits = max(3, int(height * 0.003))

    rows = np.where(hits.sum(axis=1) >= min_row_hits)[0]
    cols = np.where(hits.sum(axis=0) >= min_col_hits)[0]
    if not len(rows) or not len(cols):
        return None

    pad_x = int(width * margin_ratio)
    pad_y = int(height * margin_ratio)
    x0 = max(0, int(cols[0]) - pad_x)
    y0 = max(0, int(rows[0]) - pad_y)
    x1 = min(width, int(cols[-1]) + 1 + pad_x)
    y1 = min(height, int(rows[-1]) + 1 + pad_y)

    # 退化尺寸：太小说明找到的多半是噪点，不是内容
    if (x1 - x0) < 32 or (y1 - y0) < 32:
        return None

    # 裁出来的区域必须**包含画面里绝大部分"墨迹"**，否则说明背景判断有问题
    # （例如边框是深色、内容也是深色），此时裁下去会把正文一起切掉。
    #
    # 这里刻意不按"占整图面积的比例"来判断：内容本来就可能是窄的一条
    # （一列文字、一张小票、远处的表格），而那恰恰是最需要裁边的情形。
    # 早期版本写的是"宽度不足整图的 15% 就不裁"，把窄内容全部拒之门外。
    total_mass = float(deviation.sum())
    if total_mass > 0:
        inside_mass = float(deviation[y0:y1, x0:x1].sum())
        if inside_mass / total_mass < 0.5:
            return None

    # 本来就没什么留白，裁了也没有收益，反而多一次重编码
    if (x1 - x0) * (y1 - y0) >= width * height * 0.99:
        return None

    return (x0, y0, x1, y1)
