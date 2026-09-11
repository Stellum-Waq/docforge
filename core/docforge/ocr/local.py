"""本地离线 OCR 引擎（RapidOCR + ONNXRuntime）。

对应需求 8 的"保留本地部署选项"，也是本工具的**默认**识别引擎：
完全离线、零费用、断网可用、文件不出本机。

## 为什么选 rapidocr-onnxruntime 而不是 rapidocr 3.x

实测 3.x 依赖 ``omegaconf``，而它要求 ``antlr4-python3-runtime==4.9.*``，
国内镜像只镜像了 4.11+，导致 pip 回退到 2020 年的 omegaconf 2.0.0，
进而在启动时抛 ``UnsupportedValueType: WindowsPath is not a supported primitive type``。

对桌面软件来说另一个关键优势：**模型随包分发**，装完即可离线使用，
不需要首次运行时联网下载模型（那在企业内网里会直接失败）。

## 线程安全

ONNX Runtime 的会话不是为并发调用设计的，而任务队列用的是线程池，
多个 OCR 任务可能同时进来。因此这里用一把模块级锁把推理串行化 ——
CPU 推理本来就是算力瓶颈，并发也快不了多少，反而容易触发内存峰值。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps

from .base import OcrEngine, OcrError, OcrOptions, OcrResult, OcrUnavailable, ProgressCallback, TextBlock
from .tiling import sort_reading_order

# 单例与推理锁
_ENGINE: Any | None = None
_ENGINE_LOCK = threading.Lock()
_INFER_LOCK = threading.Lock()

# 检测预处理时把长边限制在这个尺寸内。
# 检测阶段只需定位文字行，不需要认出文字，因此缩小能显著提速；
# 得到的框再按比例还原回原图坐标。
_DETECT_MAX_SIDE = 1600


def _looks_like_point(value: Any) -> bool:
    """判断 ``value`` 是否形如一个坐标点 ``[x, y]``。"""
    return (
        isinstance(value, (list, tuple))
        and len(value) >= 2
        and isinstance(value[0], (int, float))
        and not isinstance(value[0], bool)
    )


def _box_from_detect_item(item: Any) -> Any | None:
    """从检测阶段的输出项里取出多边形。

    ## 为什么要做这层兼容（这是一个真实缺陷的修复）

    RapidOCR 在不同参数下返回的结构**不一样**：

    * ``use_rec=True``  → ``[(box, text, score), ...]``
    * ``use_rec=False`` → ``[box, box, ...]``（只有多边形，没有文本与分数）

    ``detect_lines`` 出于提速只做检测（``use_rec=False``），却按"三元组"去解析，
    于是 ``box`` 拿到了第一个点、``text`` 拿到了第二个点、
    ``score`` 拿到了第三个点 —— 解析出来的"文本"是个坐标，
    再被 :func:`_boxes_to_blocks` 当非法数据丢掉。

    结果是 **``detect_lines`` 永远返回 0 个框**。它的下游是"切片要不要切、
    切多细"的唯一输入，所以这个 bug 让整套自适应切片一直在用**猜**的字号做决策：
    实测表现为一张 784×354 的干净表格被判成"字号 6px"、竖着切成两半，
    整列数据丢失。
    """
    if not item:
        return None
    first = item[0]
    # item 本身就是多边形：[ [x,y], [x,y], ... ]
    if _looks_like_point(first):
        return item
    # item 是 (box, text, score)：取第 0 项
    if isinstance(first, (list, tuple, np.ndarray)):
        return first
    return None


def _load_engine() -> Any:
    """惰性加载引擎。模型加载约 0.5s，不该拖慢内核启动。"""
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE

    with _ENGINE_LOCK:
        if _ENGINE is not None:
            return _ENGINE
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as err:  # pragma: no cover - 依赖缺失路径
            raise OcrUnavailable(
                "未安装本地 OCR 组件。请执行：pip install rapidocr-onnxruntime onnxruntime"
            ) from err

        try:
            _ENGINE = RapidOCR()
        except Exception as err:  # noqa: BLE001 - 模型损坏等都要给出可读原因
            raise OcrUnavailable(f"本地 OCR 引擎初始化失败：{err}") from err
        return _ENGINE


def _to_rgb_array(image_path: Path | str) -> tuple[np.ndarray, int, int]:
    """读图并转成 RGB ndarray。

    刻意不用 ``cv2.imread``：它读不了 HEIC（iPhone 照片常见格式），
    而 PIL 配合 pillow-heif 可以。同时顺手按 EXIF 摆正方向，
    否则手机拍的文档会被横过来识别，准确率大幅下降。
    """
    try:
        with Image.open(image_path) as opened:
            image = ImageOps.exif_transpose(opened)
            image = image.convert("RGB")
            array = np.asarray(image)
            return array, image.width, image.height
    except OSError as err:
        raise OcrError(f"无法读取图片：{err}") from err


def _boxes_to_blocks(result: Any, scale: float = 1.0) -> list[TextBlock]:
    """把 RapidOCR 的原始输出转成统一的 TextBlock。

    RapidOCR 返回 ``[(box, text, score), ...]``，其中 box 是四点多边形；
    这里取外接矩形，并把坐标按 ``scale`` 还原回原图尺度。
    """
    blocks: list[TextBlock] = []
    if not result:
        return blocks

    for item in result:
        try:
            box, text, score = item[0], item[1], item[2]
        except (IndexError, TypeError):
            continue
        if not text or not str(text).strip():
            continue

        try:
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
        except (TypeError, ValueError, IndexError):
            continue

        x0 = int(round(min(xs) * scale))
        y0 = int(round(min(ys) * scale))
        x1 = int(round(max(xs) * scale))
        y1 = int(round(max(ys) * scale))
        if x1 <= x0 or y1 <= y0:
            continue

        try:
            confidence = float(score)
        except (TypeError, ValueError):
            confidence = 1.0

        blocks.append(TextBlock(text=str(text), box=(x0, y0, x1, y1), confidence=confidence))

    return blocks


class RapidOcrEngine(OcrEngine):
    id = "ocr.rapidocr"
    label = "RapidOCR (本地离线)"
    offline = True
    fidelity = 78

    def availability(self) -> tuple[bool, str | None]:
        import importlib.util

        missing = [
            name
            for name in ("rapidocr_onnxruntime", "onnxruntime", "cv2", "numpy")
            if importlib.util.find_spec(name) is None
        ]
        if missing:
            return False, f"缺少组件：{', '.join(missing)}"
        return True, None

    # ------------------------------------------------------------------ #
    # 版面检测（只定位文字行，不识别内容）                                 #
    # ------------------------------------------------------------------ #

    def detect_lines(self, image_path: Path | str, progress: ProgressCallback | None = None) -> list[TextBlock]:
        """只做检测，返回文字行框（坐标为原图尺度）。

        用途是给切片规划提供"字号"输入。混合模式下还会用它先做一次版面分析，
        再决定怎么切片送给云端 —— 比机械网格切图准确得多，也更省 token。
        """
        available, reason = self.availability()
        if not available:
            raise OcrUnavailable(reason or "本地 OCR 不可用")

        if progress:
            progress(10, "版面分析")

        array, width, height = _to_rgb_array(image_path)

        scale = 1.0
        long_side = max(width, height)
        if long_side > _DETECT_MAX_SIDE:
            scale = long_side / _DETECT_MAX_SIDE
            array = np.asarray(
                Image.fromarray(array).resize(
                    (max(1, int(width / scale)), max(1, int(height / scale))),
                    Image.Resampling.BILINEAR,
                )
            )

        engine = _load_engine()
        with _INFER_LOCK:
            try:
                result, _elapsed = engine(array, use_det=True, use_cls=False, use_rec=False)
            except Exception as err:  # noqa: BLE001
                raise OcrError(f"版面检测失败：{err}") from err

        if progress:
            progress(30, "版面分析完成")

        # 检测阶段只产出框，给个占位文本，行高计算即可正常工作。
        # 注意用 _box_from_detect_item 做结构兼容 —— 纯检测模式下 RapidOCR
        # 返回的是裸多边形而不是 (box, text, score)，直接按下标取会取到坐标点，
        # 最终一个框都留不下。详见该函数的说明。
        pairs: list[list[Any]] = []
        for item in result or []:
            box = _box_from_detect_item(item)
            if box is not None:
                pairs.append([box, "·", 1.0])

        return _boxes_to_blocks(pairs, scale=scale)

    # ------------------------------------------------------------------ #
    # 完整识别                                                            #
    # ------------------------------------------------------------------ #

    def recognize(
        self,
        image_path: Path | str,
        options: OcrOptions,
        progress: ProgressCallback | None = None,
    ) -> OcrResult:
        available, reason = self.availability()
        if not available:
            raise OcrUnavailable(reason or "本地 OCR 不可用")

        started = time.monotonic()
        if progress:
            progress(10, "读取图片")

        array, width, height = _to_rgb_array(image_path)

        if progress:
            progress(35, "识别中")

        engine = _load_engine()
        with _INFER_LOCK:
            try:
                result, _elapsed = engine(array)
            except Exception as err:  # noqa: BLE001
                raise OcrError(f"本地识别失败：{err}") from err

        if progress:
            progress(85, "整理结果")

        blocks = sort_reading_order(_boxes_to_blocks(result))

        if not blocks:
            return OcrResult(
                blocks=[],
                engine=self.id,
                width=width,
                height=height,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                warnings=["未识别到任何文字，请确认图片包含清晰文本"],
            )

        if progress:
            progress(100, "完成")

        return OcrResult(
            blocks=blocks,
            engine=self.id,
            width=width,
            height=height,
            language=options.language,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            tiles=1,
        )


_engine_instance: RapidOcrEngine | None = None


def get_engine() -> RapidOcrEngine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = RapidOcrEngine()
    return _engine_instance
