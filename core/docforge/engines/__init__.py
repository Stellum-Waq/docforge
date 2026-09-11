"""引擎层：可插拔的转换/识别后端。

对外只暴露一个稳定的门面，内部按设计文档 §2.4 的引擎路由表择优调用。
"""

from .probe import EngineProbe, probe_engines

__all__ = ["EngineProbe", "probe_engines"]
