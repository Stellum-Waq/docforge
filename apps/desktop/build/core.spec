# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir 模式）。

## 为什么用 onedir 而不是 onefile

onefile 每次启动都要把几百 MB 内容解压到临时目录，冷启动要十几秒；
onedir 直接就地加载，启动快得多。代价是安装目录里文件较多，
但对安装包来说无所谓。

## 收集要点

* **rapidocr 的 ONNX 模型是包数据**，不显式收集会得到一个"装好了但没有模型"
  的引擎，运行时报错还很难定位。
* **uvicorn 的 loop/protocol 实现是动态导入的**，静态分析看不到，必须手动列。
* **pywin32 的 COM 支持靠动态导入**（`win32com.client` 在运行时生成 gen_py），
  同样需要显式声明。

排除项的目的是压体积：tkinter / matplotlib / Qt 绑定 / Jupyter 这些
依赖链里带进来但从不会被用到的大家伙。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

SPEC_DIR = Path(SPECPATH).resolve()  # noqa: F821 - PyInstaller 注入
REPO = SPEC_DIR.parent.parent.parent
CORE = REPO / "core"

# --------------------------------------------------------------------------- #
# 数据与二进制                                                                  #
# --------------------------------------------------------------------------- #

datas = []
binaries = []

# rapidocr-onnxruntime 把检测/识别/方向分类的 ONNX 模型作为包数据分发。
# 不收集的话程序能启动，但一用 OCR 就报找不到模型。
datas += collect_data_files("rapidocr_onnxruntime", include_py_files=False)

# onnxruntime 的原生 DLL
binaries += collect_dynamic_libs("onnxruntime")
# opencv 的原生 DLL
binaries += collect_dynamic_libs("cv2")

# --------------------------------------------------------------------------- #
# 隐藏导入                                                                      #
# --------------------------------------------------------------------------- #

hiddenimports = [
    # uvicorn 动态加载 loop / http 协议 / lifespan 实现
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # pywin32：COM 自动化相关模块靠运行时动态导入
    "win32com",
    "win32com.client",
    "win32com.client.dynamic",
    "win32com.shell",
    "pythoncom",
    "pywintypes",
    "win32timezone",
    "win32api",
    "win32con",
    # OCR
    "rapidocr_onnxruntime",
    "onnxruntime",
    "onnxruntime.capi",
    "onnxruntime.capi._pybind_state",
    # 文档处理
    "pdf2docx",
    "fitz",
    "pymupdf",
    "docx",
    "openpyxl",
    "PIL",
    "PIL._tkinter_finder",
    # HEIC/HEIF 支持：pillow-heif 只在运行时被注册、没有静态 import，
    # 不显式列出来的话开发环境能用、打包后却报"不支持 HEIC"（实测踩过）。
    "pillow_heif",
    "multipart",
    "aiosqlite",
    "httpx",
    "anyio",
]

hiddenimports += collect_submodules("rapidocr_onnxruntime")

# --------------------------------------------------------------------------- #
# 排除                                                                          #
# --------------------------------------------------------------------------- #

# 这些依赖链里会被带进来、但本程序从不使用，排除后能省下几百 MB。
excludes = [
    "tkinter",
    "_tkinter",
    "matplotlib",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "wx",
    "IPython",
    "jupyter",
    "notebook",
    "nbformat",
    "pytest",
    "sphinx",
    "setuptools",
    "pkg_resources",
    "pip",
    "wheel",
    "pandas.tests",
    "numpy.tests",
    "scipy",
    "sklearn",
    "torch",
    "tensorflow",
    "paddle",
]


a = Analysis(  # noqa: F821 - PyInstaller 注入
    [str(CORE / "run_core.py")],
    pathex=[str(CORE)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="docforge-core",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX 压缩过的可执行文件常被杀软误报，桌面软件不值得冒这个风险
    console=True,  # 需要 stdout 与 Electron 做握手 + 传日志
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="docforge-core",
)
