"""集中式配置与路径管理。

所有可变状态都放在用户的 AppData 目录下，而不是项目目录 —— 这样打包安装后
程序目录保持只读，用户数据也不会因为重装而丢失。
"""

from __future__ import annotations

import os
import sys
import tempfile
from functools import lru_cache
from pathlib import Path


def _default_data_dir() -> Path:
    """返回用户级数据目录（Windows: %APPDATA%\\DocForge）。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "DocForge"


def _ensure_writable(path: Path) -> tuple[Path, str | None]:
    """确保数据目录可写，不可写时逐级降级。

    这一点很重要：如果 %APPDATA% 因组策略、企业环境或权限问题不可写，
    内核不应该直接崩溃 —— 那会让用户看到"启动失败"却毫无线索。
    这里依次尝试：目标目录 → 用户临时目录 → 当前工作目录下的 .docforge-data，
    并把降级原因记录下来，供自检向导展示。
    """
    candidates = [path]
    candidates.append(Path(tempfile.gettempdir()) / "DocForge")
    candidates.append(Path.cwd() / ".docforge-data")

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            if candidate != path:
                return candidate, f"首选数据目录 {path} 不可写，已改用 {candidate}"
            return candidate, None
        except OSError:
            continue

    raise RuntimeError(
        f"无法创建可写的数据目录，已尝试：{[str(c) for c in candidates]}"
    )


class Settings:
    """运行期配置。刻意保持轻量，避免引入 pydantic-settings 的额外启动开销。"""

    def __init__(self) -> None:
        self.app_name = "文枢 DocForge"
        self.version = "0.1.0"
        self.dev_mode = "--dev" in sys.argv

        self.data_dir = Path(os.environ.get("DOCFORGE_DATA_DIR") or _default_data_dir())
        self.data_dir, self.data_dir_warning = _ensure_writable(self.data_dir)

        # 子目录
        self.db_path = self.data_dir / "docforge.db"
        self.cache_dir = self.data_dir / "cache"          # OCR/转换结果缓存（省 API 费用）
        self.log_dir = self.data_dir / "logs"
        self.temp_dir = self.data_dir / "temp"            # 原子写入的中间产物
        self.preset_dir = self.data_dir / "presets"       # 任务预设（配方）
        self.asset_dir = self.data_dir / "assets"         # 用户上传的水印 Logo 等

        for d in (self.cache_dir, self.log_dir, self.temp_dir, self.preset_dir, self.asset_dir):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError:
                # 子目录建不出来不致命：相应功能（如缓存）会自行退化为"不缓存"。
                pass

        # 默认输出目录：优先桌面，其次「文档」，最后落到数据目录。
        # 企业环境下桌面常被重定向到网络盘而不可写，所以必须逐个试。
        self.default_output_dir = self._pick_output_dir()

        # 由 Electron 主进程注入，用于父进程消失时自杀，避免孤儿进程
        parent_pid = os.environ.get("DOCFORGE_PARENT_PID")
        self.parent_pid = int(parent_pid) if parent_pid and parent_pid.isdigit() else None

        self.host = "127.0.0.1"   # 硬编码：绝不对外暴露
        self.port = 0             # 0 = 由操作系统分配空闲端口

    def _pick_output_dir(self) -> Path:
        """挑选默认输出目录。

        若 ``DOCFORGE_DATA_DIR`` 被显式指定（开发模式与自动化测试都会这么做），
        输出就留在数据目录下 —— 否则开发调试会往用户真实桌面反复写文件，
        几轮测试下来桌面就乱了。生产运行时才使用桌面。
        """
        candidates: list[Path] = []
        if os.environ.get("DOCFORGE_DATA_DIR"):
            candidates.append(self.data_dir / "output")

        candidates += [
            Path.home() / "Desktop" / "DocForge输出",
            Path.home() / "Documents" / "DocForge输出",
            self.data_dir / "output",
        ]

        for candidate in candidates:
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                probe = candidate / ".write-probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
                return candidate
            except OSError:
                continue
        # 全都不可写时仍返回首选路径，让上层在真正写文件时报出明确错误
        return candidates[0]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
