"""列出已注册动作与引擎可用性。

排查"为什么某个功能用不了"时第一个该跑的命令。

用法：
    core/.venv/Scripts/python.exe tools/probe.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from docforge.actions import list_actions  # noqa: E402
from docforge.engines.probe import probe_engines  # noqa: E402
from docforge.ocr import engine_status  # noqa: E402

DOMAIN_LABEL = {
    "office": "Office 转换",
    "pdf": "PDF 处理",
    "image": "图像处理",
    "ocr": "文字识别",
    "archive": "压缩归档",
}


def main() -> None:
    print("=" * 72)
    print("已注册动作")
    print("=" * 72)
    by_domain: dict[str, list] = {}
    for spec in list_actions():
        by_domain.setdefault(spec.domain, []).append(spec)

    for domain in ("image", "pdf", "office", "ocr"):
        items = by_domain.get(domain, [])
        if not items:
            continue
        print(f"\n[{DOMAIN_LABEL.get(domain, domain)}]")
        for spec in items:
            aggregate = "  ← 聚合（整批一个结果）" if spec.aggregate else ""
            accepted = f"{len(spec.accepts)} 种格式" if spec.accepts else "任意文件"
            print(f"  {spec.id:<20} {spec.label:<14} {accepted}{aggregate}")
            print(f"  {'':<20} {spec.description}")

    print()
    print("=" * 72)
    print("引擎可用性")
    print("=" * 72)
    for probe in probe_engines(force=True):
        mark = "可用" if probe.available else "不可用"
        preferred = "  ★首选" if probe.preferred else ""
        print(f"  [{mark}] {probe.label:<32} 保真 {probe.fidelity:>3}{preferred}")
        if not probe.available and probe.reason:
            print(f"           └─ {probe.reason}")

    print()
    print("=" * 72)
    print("OCR 引擎")
    print("=" * 72)
    for item in engine_status():
        mark = "可用" if item["available"] else "不可用"
        offline = "离线" if item["offline"] else "云端"
        print(f"  [{mark}] {item['label']:<28} {offline}")
        if not item["available"] and item["reason"]:
            print(f"           └─ {item['reason']}")


if __name__ == "__main__":
    main()
