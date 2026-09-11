"""动作注册表。

导入本包即完成所有内置动作的注册 —— 新增功能只需要在这里加一行 import。
任务队列与 UI 都通过注册表发现可用能力，不需要硬编码功能清单。
"""

from .base import (  # noqa: F401
    ActionCancelled,
    ActionContext,
    ActionError,
    ActionSpec,
    TaskResult,
    atomic_write,
    get_action,
    list_actions,
    register,
    release_output_path,
    resolve_output_path,
)

# --- 内置动作（import 副作用即注册）-------------------------------------- #
from . import doc_convert  # noqa: F401,E402
from . import image_ocr  # noqa: F401,E402
from . import image_table  # noqa: F401,E402
from . import image_watermark  # noqa: F401,E402
from . import pdf_searchable  # noqa: F401,E402
from . import pdf_toolbox  # noqa: F401,E402
from . import pdf_watermark  # noqa: F401,E402
from . import pipeline  # noqa: F401,E402
from .pipeline import PipelineStep, describe_chain, parse_steps, validate_chain  # noqa: F401,E402

__all__ = [
    "ActionCancelled",
    "ActionContext",
    "ActionError",
    "ActionSpec",
    "PipelineStep",
    "TaskResult",
    "atomic_write",
    "describe_chain",
    "get_action",
    "list_actions",
    "parse_steps",
    "register",
    "release_output_path",
    "resolve_output_path",
    "validate_chain",
]
