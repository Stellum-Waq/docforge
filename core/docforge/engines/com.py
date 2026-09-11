"""Office COM 自动化引擎。

对应设计文档 §2.4 的引擎路由表：本机装了 Office 时，Word/Excel/PPT → PDF
走 COM 是**保真度最高**的路径（实测本机 Word COM 可用，保真度评 100）。

## 三个必须处理好的工程问题

**① COM 是 STA（单线程套间）。**
COM 对象只能在创建它的线程上使用。任务队列用的是线程池，如果在池里的任意线程
创建 Word 对象，会得到各种难以复现的报错。因此这里给每个 Office 应用配一条
**专用工作线程**，所有 COM 调用都排进它的队列串行执行。

**② Office 进程卡死是常态。**
弹出「宏安全性」对话框、等待用户输入、文档损坏，都会让自动化调用永久挂起。
因此每个调用都带**看门狗超时**，超时后强杀进程并重建 COM 对象。

**③ 强杀时绝不能误杀用户自己打开的文档。**
这是本模块最需要小心的地方。做法是：创建 COM 对象**之前**先记录该进程的现有 PID，
之后只杀「新出现的」那些进程 —— 也就是明确由我们拉起来的。
如果用户此时正开着 Word 写文档，我们一个都不会碰。
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any

import psutil

#: 默认看门狗超时（秒）。Word 打开大文档 + 导出 PDF 可能要几十秒，
#: 给足余量；真正卡死的情况才会触发。
DEFAULT_TIMEOUT = 240.0
#: 启动 Office 应用的等待上限
LAUNCH_TIMEOUT = 90.0


class ComError(Exception):
    """COM 自动化的可预期失败。"""


class ComUnavailable(ComError):
    """Office 未安装或 COM 组件不可用。路由层据此降级到下一个引擎。"""


class ComTimeout(ComError):
    """调用超时，进程已被强杀。"""


@dataclass
class _Job:
    func: Callable[[Any], Any]
    future: Future
    generation: int


@dataclass
class ComAppWorker:
    """某个 Office 应用的专用 COM 工作线程。"""

    prog_id: str
    process_name: str
    label: str

    _queue: "queue.Queue[_Job | None]" = field(default_factory=queue.Queue, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _app: Any = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    #: 我们创建 COM 对象之前就已存在的进程 PID。这些进程永远不碰。
    _foreign_pids: set[int] = field(default_factory=set, init=False)
    #: 我们拉起来的进程 PID
    _owned_pids: set[int] = field(default_factory=set, init=False)
    _generation: int = field(default=0, init=False)
    _last_error: str | None = field(default=None, init=False)

    # ------------------------------------------------------------------ #
    # 对外接口                                                            #
    # ------------------------------------------------------------------ #

    def run(self, func: Callable[[Any], Any], timeout: float = DEFAULT_TIMEOUT) -> Any:
        """在 COM 线程上执行 ``func(app)``，带看门狗超时。"""
        self._ensure_thread()
        generation = self._generation
        future: Future = Future()
        self._queue.put(_Job(func=func, future=future, generation=generation))

        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            # 超时：强杀我们自己拉起来的进程，让 COM 调用立刻失败返回，
            # 工作线程得以从挂起中恢复并处理后续任务。
            killed = self._kill_owned_processes()
            self._invalidate()
            detail = f"，已强制结束进程（PID {killed}）" if killed else ""
            raise ComTimeout(f"{self.label} 处理超时（超过 {int(timeout)} 秒）{detail}") from None
        except Exception as err:  # noqa: BLE001 - 统一转成 ComError 便于上层处理
            message = str(err)
            if "CoInitialize" in message or "automation" in message.lower():
                raise ComUnavailable(f"{self.label} 自动化不可用：{message}") from err
            raise ComError(f"{self.label} 处理失败：{message}") from err

    def shutdown(self) -> None:
        """停止工作线程并退出 Office 进程。

        **不依赖工作线程完成清理。** 实测踩过的坑：工作线程是守护线程，
        主进程退出时会被直接掐断；如果回收逻辑只写在它里面，就会出现
        "进程退了、Office 却还留着"的情况。因此这里在 join 之后，
        由**调用线程**再做一次强制清理 —— 优雅退出是锦上添花，
        "不留残留进程"才是硬要求。
        """
        with self._lock:
            thread = self._thread
            self._thread = None

        if thread is not None:
            self._queue.put(None)
            # 8 秒足够工作线程完成「Quit + 优雅等待」；超时也不能再等了，
            # 下面会直接用强制手段收尾。
            thread.join(timeout=8.0)

        # 无论线程是否正常结束，都从当前线程收尾一次
        self._quit_app(gentle=False)
        self._kill_owned_processes()

    @property
    def last_error(self) -> str | None:
        return self._last_error

    # ------------------------------------------------------------------ #
    # 工作线程                                                            #
    # ------------------------------------------------------------------ #

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._loop, name=f"com-{self.prog_id}", daemon=True
            )
            self._thread.start()

    def _loop(self) -> None:
        import pythoncom

        # 每个线程都必须自己初始化 COM 套间，否则调用会报
        # "CoInitialize has not been called"
        pythoncom.CoInitialize()
        try:
            while True:
                job = self._queue.get()
                if job is None:
                    break

                # 过期任务（所属的 generation 已被超时作废）直接丢弃
                if job.generation != self._generation:
                    if not job.future.done():
                        job.future.set_exception(ComTimeout("该次调用已因超时被作废"))
                    continue

                app = None
                try:
                    app = self._get_app()
                    result = job.func(app)
                    if not job.future.done():
                        job.future.set_result(result)
                except BaseException as err:  # noqa: BLE001 - 含 Windows 级异常
                    self._last_error = str(err)

                    # 这一段是**顺序敏感**的清理，踩过两次，别再调换：
                    #
                    # 1) 先丢掉本线程对 app 的引用（_loop 的局部变量）。
                    # 2) 再摘掉异常上的 traceback —— traceback 里的栈帧同样持有
                    #    app。异常对象会被存进 Future，而那个 Future 可能在**别的
                    #    线程**上被回收，于是 COM 代理的 Release() 会发生在没有
                    #    CoInitialize 的线程上，Windows 抛 0x800401f0
                    #    （CO_E_NOTINITIALIZED）。上层只需要消息文本，不需要调用栈。
                    # 3) 最后才 _invalidate()：此时 self._app 是最后一个引用，
                    #    它在工作线程上、且**服务器还活着**的时候被释放 —— 干净。
                    #    _invalidate() 随后的杀进程动作才不会踩到"向已死服务器
                    #    释放代理"，也就不会抛 0x800706ba。
                    app = None
                    try:
                        err.__traceback__ = None
                    except Exception:  # noqa: BLE001 - 少数异常类型不允许赋值
                        pass
                    self._invalidate()

                    if not job.future.done():
                        try:
                            job.future.set_exception(err)
                        except Exception:  # noqa: BLE001
                            job.future.set_exception(ComError(str(err)))
        finally:
            # finally 里也要防止异常逃逸：一旦逃逸，工作线程直接死亡，
            # 后续任务全部卡住，而且最后一次清理也不会执行。
            #
            # **这里的先后顺序是踩出来的**：必须在杀掉 Office 进程**之前**
            # 调用 CoUninitialize()。反过来的话，释放 COM 套间会向一个已经
            # 不存在的服务器发 RPC，Windows 层面抛 0x800706ba；这是 SEH 而不是
            # Python 异常，try/except 拦不住，faulthandler 会打出一大段
            # "Windows fatal exception" 堆栈（退出码不受影响，但日志很吓人，
            # 还会掩盖真正的错误）。
            try:
                self._quit_app(gentle=False, kill=False)
            except BaseException:  # noqa: BLE001
                pass
            try:
                pythoncom.CoUninitialize()
            except BaseException:  # noqa: BLE001
                pass
            try:
                # 硬要求：不留残留 Office 进程。放最后，但一定要执行。
                self._kill_owned_processes()
            except BaseException:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    # Office 应用生命周期                                                 #
    # ------------------------------------------------------------------ #

    def _get_app(self) -> Any:
        if self._app is not None:
            return self._app

        try:
            import win32com.client
            import pythoncom
        except ImportError as err:  # pragma: no cover
            raise ComUnavailable(
                "未安装 pywin32，无法使用 Office 高保真转换。请执行 pip install pywin32"
            ) from err

        # 兜底：万一还有上次遗留的"自有进程"没清掉，先收拾干净再启动新的。
        # 必须在记录 _foreign_pids **之前**做 —— 否则那个遗留进程会被当成
        # 外来进程，从此再也清理不掉（这是最阴的一种泄漏）。
        self._kill_owned_processes()

        # 记录"创建之前"已存在的进程，之后只清理新出现的，绝不碰用户的文档
        self._foreign_pids = self._current_pids()

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                app = win32com.client.DispatchEx(self.prog_id)
                break
            except pythoncom.com_error as err:
                last_error = err
                time.sleep(1.0 * (attempt + 1))
        else:
            raise ComUnavailable(
                f"无法启动 {self.label}。请确认已安装 Office 且未被安全软件拦截。（{last_error}）"
            )

        # 关键：DispatchEx 返回时 Office 进程**未必已经出现在进程列表里**
        # （Word 冷启动要一两秒）。如果这时立刻采集，会得到一个空的"自有进程"
        # 集合 —— 之后无论超时还是正常退出都杀不掉任何东西，进程就此泄漏。
        # 实测踩过这个坑，所以这里轮询等它出现。
        self._capture_owned_pids()

        try:
            # 隐藏窗口、关掉所有弹窗。这一步很关键：任何模态对话框都会让
            # 自动化永久挂起，直到看门狗超时。
            if self.prog_id.startswith("Word"):
                app.Visible = False
                app.DisplayAlerts = 0
                try:
                    app.Options.ConfirmConversions = False
                    app.Options.SaveInterval = 0
                    app.Options.WarnBeforeSavingPrintingSendingMarkup = False
                except Exception:  # noqa: BLE001 - 不同版本选项名略有差异
                    pass
                # 关键：不加载宏，避免宏安全提示阻塞自动化
                try:
                    app.AutomationSecurity = 3  # msoAutomationSecurityForceDisable
                except Exception:  # noqa: BLE001
                    pass
            elif self.prog_id.startswith("Excel"):
                app.Visible = False
                app.DisplayAlerts = False
                app.AskToUpdateLinks = False
                app.EnableEvents = False
            elif self.prog_id.startswith("PowerPoint"):
                # PowerPoint 不允许把主窗口设为不可见，只能最小化
                try:
                    app.WindowState = 2  # ppWindowMinimized
                except Exception:  # noqa: BLE001
                    pass
                app.DisplayAlerts = 1
        except Exception as err:  # noqa: BLE001
            self._last_error = f"初始化 {self.label} 失败：{err}"

        self._app = app
        return app

    def _invalidate(self) -> None:
        """丢弃当前应用对象，让下次调用重建；**并同步杀掉旧进程**。

        为什么要杀：只把引用置空的话，``DispatchEx`` 出来的那个 Office 实例
        就成了没人管的孤儿，而且会**永久**失联 —— 下次 ``_get_app()`` 会把
        "启动前已存在的进程"整体记为外来进程，那个孤儿不在 ``_owned_pids`` 里，
        此后任何清理逻辑都再也认领不到它，连进程退出时的兜底清理也救不回来。

        实测复现：连续 4 次 COM 调用失败 → 4 个残留 WINWORD.EXE，
        ``shutdown_all()`` 之后依然全部存活。
        """
        app = self._app
        self._app = None
        self._generation += 1
        # 先解引用再杀进程：反过来的话释放 COM 代理会向已死的服务器发 RPC
        del app
        self._kill_owned_processes()

    def _quit_app(self, *, gentle: bool = True, kill: bool = True) -> None:
        """优雅退出 Office 并**确保**进程被回收。

        :param gentle: True 时先给 Office 最多 6 秒自行退出（能让它保存状态、
            不留临时文件）；False 时跳过长等待，直接进入强制清理 ——
            用于内核退出这种"必须快、且必须干净"的场景。
        :param kill: 是否在最后强制回收进程。工作线程需要在
            ``CoUninitialize()`` **之前**先请求退出、之后再杀进程，
            所以它会把这一步拆成两次调用（``kill=False`` → 之后单独调
            ``_kill_owned_processes()``）。

        这里的关键是 ``finally``：COM 服务器已经失效时 ``Quit()`` 会抛
        Windows 级异常（实测 0x800706be / 0x800706ba），如果不在 finally 里
        清理进程，就正好泄漏出一个无主的 WINWORD.EXE —— 而"不留残留进程"
        是这个模块存在的主要理由之一。踩过一次，写成硬约束。
        """
        app = self._app
        self._app = None
        try:
            if app is not None:
                try:
                    app.Quit()
                except BaseException:  # noqa: BLE001 - 含 Windows 级异常
                    pass
                if gentle:
                    deadline = time.time() + 6.0
                    while time.time() < deadline:
                        if not (self._owned_pids & self._current_pids()):
                            break
                        time.sleep(0.3)
                # 先解引用，再（下面）杀进程。顺序反了的话，释放 COM 代理会向
                # 已经死掉的服务器发 RPC，Windows 直接抛 0x800706ba。
                del app
        finally:
            if kill:
                self._kill_owned_processes()

    def force_kill(self) -> list[int]:
        """兜底清理：无论线程状态如何，都把本模块拉起的 Office 进程杀掉。"""
        return self._kill_owned_processes()

    # ------------------------------------------------------------------ #
    # 进程管理（只碰自己拉起来的）                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _current_pids() -> set[int]:
        pids: set[int] = set()
        for process in psutil.process_iter(["name"]):
            name = (process.info.get("name") or "").lower()
            if name.endswith(".exe"):
                name = name[:-4]
            if name in ("winword", "excel", "powerpnt"):
                pids.add(process.pid)
        return pids

    def _capture_owned_pids(self, wait: float = 15.0) -> None:
        """采集"由我们拉起的"Office 进程 PID。

        必须在 DispatchEx 之后**轮询等待**进程真正出现，因为 Office 冷启动
        需要一两秒。等待期间每 0.2 秒检查一次，一旦发现新进程就记下来。

        依然严格限定为"创建前不存在的进程"，因此用户自己开着的 Word 文档
        永远不会被记录、也就永远不会被误杀。
        """
        deadline = time.time() + wait
        while time.time() < deadline:
            new_pids = self._current_pids() - self._foreign_pids
            if new_pids:
                self._owned_pids |= new_pids
                return
            time.sleep(0.2)

    def _kill_owned_processes(self) -> list[int]:
        """结束由本模块拉起的 Office 进程。

        **只杀我们自己创建的**：``_owned_pids`` 是"创建 COM 对象之后新出现的"
        进程集合。用户此前就开着的 Word 文档不在其中，因此绝不会被波及 ——
        误杀用户未保存的文档是绝对不能接受的事故。

        这里刻意**不信任单次 kill() 的返回值**：实测遇到过 Word 正在退出的瞬间
        ``psutil.Process(pid)`` 抛 ``NoSuchProcess``（进程实际还活着），结果集合被
        清空却什么都没杀掉，进程就此泄漏。因此改为"调用 → 验证是否真的消失 →
        没消失就重试 → 仍不行则降级到 taskkill"。
        """
        if not self._owned_pids:
            return []

        killed: list[int] = []

        for attempt in range(3):
            pending = sorted(self._owned_pids - set(killed))
            if not pending:
                break
            for pid in pending:
                if self._terminate(pid, use_taskkill=attempt > 0):
                    killed.append(pid)
            if attempt < 2:
                time.sleep(0.4)

        # 杀不掉的保留在集合里，留给 force_kill / 下次重试，而不是直接丢弃
        self._owned_pids -= set(killed)
        return killed

    def _terminate(self, pid: int, *, use_taskkill: bool = False) -> bool:
        """终止单个进程，返回它是否**确认已消失**。"""
        if use_taskkill:
            self._taskkill(pid)

        try:
            psutil.Process(pid).kill()
        except psutil.NoSuchProcess:
            pass  # 可能真的已经退出，下面统一验证
        except BaseException:  # noqa: BLE001
            pass

        # 统一以"进程枚举里还在不在"作为判据 —— 比 wait() 更可靠，
        # 因为它不会受进程正在退出这种瞬时状态影响。
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if pid not in self._current_pids():
                return True
            time.sleep(0.2)

        if not use_taskkill:
            self._taskkill(pid)
            time.sleep(0.5)
            return pid not in self._current_pids()
        return False

    def _taskkill(self, pid: int) -> None:
        """降级到 Windows 自带的 taskkill。

        ``/T`` 一并结束子进程：Office 有时会拉起辅助进程，
        只结束主进程会留下一堆半死不活的残留。
        """
        import subprocess

        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except BaseException:  # noqa: BLE001 - 兜底手段失败也无妨
            pass


# --------------------------------------------------------------------------- #
# 全局单例                                                                      #
# --------------------------------------------------------------------------- #

_WORKERS: dict[str, ComAppWorker] = {}
_WORKERS_LOCK = threading.Lock()

#: 进程名 → (ProgID, 展示名)
APPLICATIONS = {
    "word": ("Word.Application", "winword", "Microsoft Word"),
    "excel": ("Excel.Application", "excel", "Microsoft Excel"),
    "powerpoint": ("PowerPoint.Application", "powerpnt", "Microsoft PowerPoint"),
}


def get_worker(name: str) -> ComAppWorker:
    """按应用名获取（或创建）COM 工作线程。"""
    if name not in APPLICATIONS:
        raise ComError(f"未知的 Office 应用：{name}")

    with _WORKERS_LOCK:
        worker = _WORKERS.get(name)
        if worker is None:
            prog_id, process_name, label = APPLICATIONS[name]
            worker = ComAppWorker(prog_id=prog_id, process_name=process_name, label=label)
            _WORKERS[name] = worker
        return worker


def has_any_worker() -> bool:
    with _WORKERS_LOCK:
        return any(worker._app is not None for worker in _WORKERS.values())


def shutdown_all() -> None:
    """退出前清理所有 Office 进程。

    内核关闭时必须调用，否则会在用户机器上留下无主的 WINWORD.EXE ——
    这是 COM 自动化最常见、也最惹人烦的后遗症。

    这里对每个 worker 都做**两层清理**：先走正常的 shutdown，再用
    ``force_kill`` 兜底。原因是 shutdown 内部会调用 COM 的 ``Quit()``，
    而 COM 服务器失效时可能抛出 Windows 级异常并直接终止工作线程 ——
    没有第二层兜底的话，进程就留在那里了。
    """
    with _WORKERS_LOCK:
        workers = list(_WORKERS.values())
        _WORKERS.clear()

    for worker in workers:
        try:
            worker.shutdown()
        except BaseException:  # noqa: BLE001 - 含 Windows 级异常，必须继续下一个
            pass
        finally:
            try:
                worker.force_kill()
            except BaseException:  # noqa: BLE001
                pass
