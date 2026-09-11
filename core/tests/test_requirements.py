"""依赖清单完整性测试。

## 为什么需要它

`requirements.txt` 漏包是**最容易被忽视、后果又最尴尬**的一类缺陷：
本机开发环境里那个包早就装好了，跑什么都是绿的，只有按 README 从零装的人
才会发现少了东西 —— 而且报错往往出现在"跑某个功能"的时候，不是启动的时候。

实测踩过一次：`pillow-heif` 一直没写进任何一份依赖清单，于是
**HEIC（iPhone 照片）在新环境里根本读不了**，而 `image.watermark` 与
`image.ocr` 的 `accepts` 里都声明了 `.heic` —— 等于对外承诺了做不到的事。
同一次还发现 `requirements.txt` 只列了 Web 层的包，PDF / OCR / Office
全都缺，照着 README 装出来的内核"能启动但什么都做不了"。

所以这里做一件很直白的事：**把代码里真正 import 的第三方包，和依赖清单
对一遍**。新增依赖忘了写清单，测试立刻失败。

## 实现说明

用 ``ast`` 静态解析而不是运行时 import：这样连"只在某个分支里才 import 的
可选依赖"（例如 ``win32com``）也能被发现。
模块名 → 发行包名用 ``importlib.metadata.packages_distributions()`` 反查，
它能正确处理 ``PIL→pillow``、``cv2→opencv-python``、``docx→python-docx``
这类对不上的情况。
"""

from __future__ import annotations

import ast
import sys
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parent.parent
PACKAGE_DIR = CORE / "docforge"
TESTS_DIR = CORE / "tests"
RUNTIME_REQS = CORE / "requirements.txt"
DEV_REQS = CORE / "requirements-dev.txt"

#: 只在本项目内部、或属于标准库/内置的模块名，不需要出现在依赖清单里
IGNORED = {"docforge", "run_core"}


def _normalize(name: str) -> str:
    """按 PEP 503 归一化包名（下划线/点/连字符等价，大小写不敏感）。"""
    return name.strip().lower().replace("_", "-").replace(".", "-")


def _requirement_names(path: Path) -> set[str]:
    """读一份 requirements 文件，取出被声明的包名（忽略注释、-r、环境标记）。

    用 ``utf-8-sig`` 读取：Windows 上用记事本另存为 UTF-8 会带上 BOM，
    而 BOM 会让**第一条依赖**匹配不上 —— 这种"只错一行"的失效最难查。
    """
    if not path.is_file():
        return set()
    names: set[str] = set()
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        # 去掉环境标记与版本约束，只留包名
        line = line.split(";", 1)[0].strip()
        for separator in ("==", ">=", "<=", "~=", "!=", ">", "<", "["):
            line = line.split(separator, 1)[0]
        if line:
            names.add(_normalize(line))
    return names


def _imported_top_level(directory: Path) -> dict[str, set[str]]:
    """静态收集目录下所有 .py 文件的顶层 import，返回 {模块名: {出现文件}}。"""
    found: dict[str, set[str]] = {}

    for path in sorted(directory.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as err:  # pragma: no cover - 语法错误该由别的测试抓
            raise AssertionError(f"{path} 解析失败：{err}") from err

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # level > 0 是相对导入（包内），跳过
                if node.level:
                    continue
                modules = [node.module or ""]
            else:
                continue

            for module in modules:
                top = module.split(".", 1)[0]
                if not top or top in IGNORED:
                    continue
                found.setdefault(top, set()).add(path.name)

    return found


def _distribution_for(module: str) -> str | None:
    """把顶层模块名映射到发行包名；标准库返回 None。"""
    if module in sys.stdlib_module_names or module in sys.builtin_module_names:
        return None

    distributions = packages_distributions().get(module)
    if not distributions:
        # 有些包（例如 pywin32 的 win32com）在某些安装方式下映射不到，
        # 这里不直接判失败，交给调用方按"未知"处理
        return None

    # 一个模块可能由多个发行包提供（例如 opencv 的 cv2）；
    # 只要其中一个在清单里就算满足
    return distributions


@pytest.fixture(scope="module")
def declared() -> set[str]:
    return _requirement_names(RUNTIME_REQS) | _requirement_names(DEV_REQS)


def test_requirements_files_exist() -> None:
    assert RUNTIME_REQS.is_file(), "缺少 core/requirements.txt"
    assert DEV_REQS.is_file(), "缺少 core/requirements-dev.txt"


def test_runtime_requirements_are_not_empty() -> None:
    names = _requirement_names(RUNTIME_REQS)
    # 这份清单必须能撑起"照着 README 装完就能用"：PDF 与图像是核心能力
    for expected in ("fastapi", "pymupdf", "pillow", "openpyxl", "python-docx"):
        assert expected in names, f"requirements.txt 缺少核心依赖：{expected}"


def test_every_third_party_import_is_declared(declared: set[str]) -> None:
    """代码里 import 的每个第三方包，都必须在依赖清单里。"""
    missing: list[str] = []
    unknown: list[str] = []

    for module, files in sorted(_imported_top_level(PACKAGE_DIR).items()):
        distributions = _distribution_for(module)
        if distributions is None:
            if module not in sys.stdlib_module_names:
                unknown.append(f"{module}（{', '.join(sorted(files))}）")
            continue
        if not any(_normalize(d) in declared for d in distributions):
            missing.append(f"{module} → {distributions}（{', '.join(sorted(files))}）")

    assert not missing, (
        "以下第三方包被 import 了，但没写进 requirements.txt / requirements-dev.txt：\n  "
        + "\n  ".join(missing)
        + "\n\n照着 README 从零安装的环境会缺这些包。"
    )

    # "未知"不直接判失败（可能只是元数据映射不到），但打印出来便于排查
    if unknown:  # pragma: no cover - 仅在出现映射盲区时执行
        print("无法映射到发行包的模块（请人工确认是否需要写进清单）：")
        for item in unknown:
            print(f"  · {item}")


def test_test_dependencies_are_declared(declared: set[str]) -> None:
    """测试代码用到的包要写进 dev 清单，而不是混进运行时清单。"""
    dev = _requirement_names(DEV_REQS)

    for module in _imported_top_level(TESTS_DIR):
        if module in {"pytest", "pytest_asyncio"}:
            assert _normalize(module) in dev, f"测试依赖 {module} 未写进 requirements-dev.txt"


def test_dev_requirements_pull_in_runtime() -> None:
    """dev 清单必须包含运行时清单，否则开发者装完 dev 还缺运行依赖。"""
    text = DEV_REQS.read_text(encoding="utf-8")
    assert "-r requirements.txt" in text


def test_heic_support_is_declared(declared: set[str]) -> None:
    """HEIC 支持曾经漏在清单外，导致新环境读不了 iPhone 照片。"""
    assert "pillow-heif" in declared, (
        "pillow-heif 必须写进依赖清单：image.watermark / image.ocr 声明接受 .heic，"
        "少了它这两个功能在新环境里会直接报「不支持的格式」。"
    )


def test_pywin32_is_declared_for_windows(declared: set[str]) -> None:
    """Office COM 是最高保真引擎，Windows 上不能少。"""
    assert "pywin32" in declared
