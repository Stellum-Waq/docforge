"""OCR 引擎抽象层。

对应设计文档 §1-G3：所有识别引擎（DeepSeek Vision 云端、RapidOCR 本地、
PaddleOCR 增强）都实现同一个 :class:`OcrEngine` 接口，上层动作只依赖这个接口，
因此"换引擎""云端失败降级本地""敏感文件强制本地"都只是路由策略的变化，
不需要改任何业务代码。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class OcrError(Exception):
    """识别过程中的可预期失败（引擎不可用、图片损坏、配额不足等）。"""


class OcrUnavailable(OcrError):
    """引擎当前不可用（未安装依赖、未配置密钥、断网）。路由层据此降级。"""


@dataclass
class TextBlock:
    """一段识别出的文字及其位置。

    坐标一律使用**原图像素坐标系**（左上角为原点），这样跨切片合并时才不会错位。
    """

    text: str
    box: tuple[int, int, int, int]  # x0, y0, x1, y1
    confidence: float = 1.0

    @property
    def center(self) -> tuple[float, float]:
        x0, y0, x1, y1 = self.box
        return ((x0 + x1) / 2, (y0 + y1) / 2)

    @property
    def line_height(self) -> int:
        return max(1, self.box[3] - self.box[1])

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "box": list(self.box), "confidence": round(self.confidence, 3)}


@dataclass
class OcrResult:
    """一次识别任务的完整结果。"""

    blocks: list[TextBlock]
    engine: str
    width: int = 0
    height: int = 0
    language: str | None = None
    elapsed_ms: int = 0
    tiles: int = 1
    usage: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # 供表格动作使用的原始结构化数据（例如模型直接返回的 markdown 表格）
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """按阅读顺序拼接的纯文本。"""
        return "\n".join(block.text for block in self.blocks if block.text.strip())

    @property
    def average_confidence(self) -> float:
        if not self.blocks:
            return 0.0
        return sum(b.confidence for b in self.blocks) / len(self.blocks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "text": self.text,
            "blocks": [b.to_dict() for b in self.blocks],
            "width": self.width,
            "height": self.height,
            "lineCount": len(self.blocks),
            "averageConfidence": round(self.average_confidence, 3),
            "elapsedMs": self.elapsed_ms,
            "tiles": self.tiles,
            "usage": self.usage,
            "warnings": self.warnings,
        }


@dataclass
class OcrOptions:
    """识别选项。字段与 UI 上的参数一一对应。"""

    # text=纯文字 / table=表格 / layout=带版面结构 / formula=公式转 LaTeX
    mode: str = "text"
    # 语言提示，仅作为提示词的一部分；auto 表示让模型自行判断
    language: str = "auto"
    # auto=按图片尺寸与字号自动决定 / off=整图送识别 / always=强制切片
    tiling: str = "auto"
    # 云端图片细节档位：low 会把图压到 512x512（更省 token）
    detail: str = "original"
    # 覆盖默认提示词模板
    prompt_override: str | None = None
    # 输出上限，防止模型在长文档上被截断
    max_tokens: int = 8192
    # 表格模式：是否把结果也整理成结构化行列表
    extract_tables: bool = True
    # 允许的引擎白名单（为空表示不限制）。用于"敏感文件强制本地"
    allowed_engines: list[str] = field(default_factory=list)
    # 云端失败时是否降级到本地引擎
    allow_fallback: bool = True


ProgressCallback = Callable[[float, str], None]


class OcrEngine(ABC):
    """识别引擎接口。"""

    #: 稳定标识，用于路由与结果缓存键
    id: str = "base"
    #: 展示名称
    label: str = "引擎"
    #: 是否完全离线（离线引擎可在断网与隐私敏感场景使用）
    offline: bool = False
    #: 保真度评分，路由时优先选高分引擎
    fidelity: int = 50

    @abstractmethod
    def availability(self) -> tuple[bool, str | None]:
        """返回 (是否可用, 不可用原因)。必须快速且不抛异常。"""

    @abstractmethod
    def recognize(
        self,
        image_path: Path | str,
        options: OcrOptions,
        progress: ProgressCallback | None = None,
    ) -> OcrResult:
        """识别一张图片。失败时抛 :class:`OcrError` 的子类。"""

    def estimate_cost(self, image_path: Path | str, options: OcrOptions) -> dict[str, Any]:
        """预估本次识别的开销（云端引擎会返回 token 估算）。默认无开销。"""
        return {}


def guess_language_hint(text: str) -> str:
    """根据文本粗略判断语言，用于给提示词加提示（不追求精确）。"""
    if not text:
        return "auto"
    cjk = sum(1 for ch in text if 0x4E00 <= ord(ch) <= 0x9FFF)
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    digits = sum(1 for ch in text if ch.isdigit())
    total = max(1, cjk + latin + digits)
    if cjk / total > 0.25:
        return "zh"
    if latin / total > 0.6:
        return "en"
    return "auto"
