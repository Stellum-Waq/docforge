"""验证打包后的内核可执行文件。

## 为什么必须单独测

PyInstaller 打包最典型的失败方式，是**程序能启动、但一动真格就报错**：
ONNX 模型没被收集进去、OpenCV 的原生 DLL 缺失、pywin32 的 COM 模块找不到。
这些在源码运行时完全正常，只有打包装出来才会暴露。

因此这个脚本不看"能不能启动"，而是**真的跑一遍各类任务**：
图片水印、PDF 水印、本地 OCR、图片转 Excel —— 覆盖所有重依赖。

用法（先执行 pnpm package:core）：
    core/.venv/Scripts/python.exe tools/test_packaged.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
EXE = REPO / "core" / "dist" / "docforge-core" / "docforge-core.exe"
WORK = REPO / ".cache" / "packaged-test"

# 生成素材时要用到内核里的小工具（例如注册 HEIC 打开器），
# 因此把 core 加进搜索路径。注意这只影响**测试脚本自身**，
# 被测对象始终是打包出来的可执行文件。
sys.path.insert(0, str(REPO / "core"))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

READY_RE = re.compile(r"@@DOCFORGE_READY@@\s*(\{.*\})")

_failures: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> bool:
    mark = "[通过]" if condition else "[失败]"
    print(f"  {mark} {label}{(' — ' + detail) if detail and not condition else ''}")
    if not condition:
        _failures.append(label)
    return condition


def make_fixtures() -> dict[str, Path]:
    """生成测试素材。刻意包含需要重依赖才能处理的类型。"""
    from PIL import Image, ImageDraw, ImageFont

    src = WORK / "src"
    src.mkdir(parents=True, exist_ok=True)

    font_path = "C:/Windows/Fonts/msyh.ttc"

    photo = src / "照片.png"
    Image.new("RGB", (1200, 900), "#2A6FB0").save(photo)

    # 一张真实的 HEIC（iPhone 默认格式），用来验证打包后仍能读取
    heic_ok = False
    try:
        from docforge.imaging import ensure_heif_support

        if ensure_heif_support():
            Image.new("RGB", (800, 600), "#1E7A4F").save(src / "iphone.heic", format="HEIF")
            heic_ok = True
    except Exception:  # noqa: BLE001
        pass

    text_image = src / "文档截图.png"
    canvas = Image.new("RGB", (1000, 360), "white")
    draw = ImageDraw.Draw(canvas)
    title = ImageFont.truetype(font_path, 38) if Path(font_path).is_file() else ImageFont.load_default()
    body = ImageFont.truetype(font_path, 26) if Path(font_path).is_file() else ImageFont.load_default()
    draw.text((50, 40), "打包后内核验证", font=title, fill="black")
    draw.text((50, 130), "办公文件转换工具", font=body, fill="black")
    draw.text((50, 190), "Packed core smoke test 2026", font=body, fill="black")
    canvas.save(text_image)

    table = src / "表格截图.png"
    sheet = Image.new("RGB", (900, 300), "white")
    pen = ImageDraw.Draw(sheet)
    cell = ImageFont.truetype(font_path, 28) if Path(font_path).is_file() else ImageFont.load_default()
    xs = [30, 300, 570, 840]
    ys = [40, 125, 210, 295]
    for y in ys:
        pen.line([(xs[0], y), (xs[-1], y)], fill="black", width=3)
    for x in xs:
        pen.line([(x, ys[0]), (x, ys[-1])], fill="black", width=3)
    for col, text in enumerate(("姓名", "部门", "金额")):
        pen.text((xs[col] + 30, ys[0] + 30), text, font=cell, fill="black")
    for col, text in enumerate(("张三", "技术部", "1200")):
        pen.text((xs[col] + 30, ys[1] + 30), text, font=cell, fill="black")
    sheet.save(table)

    import pymupdf

    pdf = src / "报告.pdf"
    doc = pymupdf.open()
    for index in range(2):
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 90), f"Packed test page {index + 1}", fontsize=18, fontname="helv")
    doc.save(str(pdf))
    doc.close()

    # 一份"扫描件"：纯图片、没有文字层。用来验证「扫描件转可搜索 PDF」
    # 在打包产物里也能栅格化 + 叠加不可见文字层（要真的 OCR，因此也顺带
    # 验证 ONNX 模型在打包后可用）。
    scan_pdf = src / "扫描件.pdf"
    scan_doc = pymupdf.open()
    scan_page = scan_doc.new_page(width=595, height=842)
    image_bytes = text_image.read_bytes()
    scan_page.insert_image(pymupdf.Rect(0, 0, 595, 842 * 0.62), stream=image_bytes)
    scan_doc.save(str(scan_pdf))
    scan_doc.close()

    # 一份 Word，用来验证流水线里最后一步决定输出扩展名
    docx_path = src / "报告.docx"
    try:
        from docx import Document

        document = Document()
        document.add_heading("打包流水线验证", level=1)
        document.add_paragraph("用于验证 docx → pdf → 水印 的链式转换。")
        document.save(str(docx_path))
    except Exception:  # noqa: BLE001
        pass

    return {
        "photo": photo,
        "text_image": text_image,
        "table": table,
        "pdf": pdf,
        "scan_pdf": scan_pdf,
        "docx": docx_path,
        "heic_ok": heic_ok,
    }


class PackagedCore:
    def __init__(self) -> None:
        self.proc: subprocess.Popen[str] | None = None
        self.port = 0
        self.token = ""

    def start(self) -> None:
        if not EXE.is_file():
            raise SystemExit(f"找不到打包产物：{EXE}\n请先执行 pnpm package:core")

        env = {
            **dict(__import__("os").environ),
            "PYTHONIOENCODING": "utf-8",
            "DOCFORGE_DATA_DIR": str(WORK / "data"),
        }
        self.proc = subprocess.Popen(
            [str(EXE)],
            cwd=str(EXE.parent),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        deadline = time.time() + 90  # 打包后首次启动要解压/加载，给足时间
        assert self.proc.stdout is not None
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                if self.proc.poll() is not None:
                    err = self.proc.stderr.read() if self.proc.stderr else ""
                    raise SystemExit(f"打包内核提前退出：\n{err}")
                continue
            match = READY_RE.search(line)
            if match:
                payload = json.loads(match.group(1))
                self.port, self.token = payload["port"], payload["token"]
                return
        raise SystemExit("打包内核未在 90s 内就绪")

    def stop(self) -> None:
        """优雅停止：先请求退出，等它自己收尾（回收 Office 进程），再兜底。"""
        if not self.proc or self.proc.poll() is not None:
            return
        try:
            with self.client as client:
                client.post("/api/shutdown")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.terminate()

    @property
    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url=f"http://127.0.0.1:{self.port}",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=300.0,
        )


def get_json(client: httpx.Client, path: str) -> dict:
    """取 JSON 响应，失败时带上响应体，便于定位（否则只能看到一句 JSONDecodeError）。"""
    response = client.get(path)
    if response.status_code != 200:
        raise SystemExit(f"GET {path} 返回 {response.status_code}：{response.text[:800]}")
    try:
        return response.json()
    except Exception as err:  # noqa: BLE001
        raise SystemExit(f"GET {path} 返回的不是 JSON：{response.text[:800]}") from err


def run_job(client: httpx.Client, action: str, files: list[Path], params: dict, out_dir: Path) -> dict:
    response = client.post(
        "/api/jobs",
        json={
            "action": action,
            "files": [str(f) for f in files],
            "outputDir": str(out_dir),
            "params": params,
        },
    )
    if response.status_code != 201:
        return {"status": f"create-failed: {response.text[:200]}"}

    job_id = response.json()["job"]["id"]
    deadline = time.time() + 300
    job: dict = {}
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()["job"]
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.3)

    # 失败时把每个文件的具体原因带出来 —— 否则只看到一句 "failed"，
    # 完全不知道是缺依赖、缺模型还是代码问题。
    if job.get("status") != "succeeded":
        try:
            detail = client.get(f"/api/jobs/{job_id}").json()
            for task in detail.get("tasks", []):
                if task.get("error"):
                    print(f"       × {task['fileName']}: {task['error'][:300]}")
        except Exception:  # noqa: BLE001
            pass

    return job


def job_all_files_succeeded(client: httpx.Client, job: dict) -> tuple[bool, str]:
    """判断任务里**每个文件**是否都真正处理成功。

    只看 job.status 是不够的：当所有文件都因格式不匹配被跳过时，
    任务状态同样是 "succeeded"（0 成功 + 1 跳过），于是断言会假通过。
    实测踩过 —— 一个 HEIC 支持的用例就这样骗过了检查，
    而实际上那张图被当成"格式不支持"直接跳过了。
    """
    job_id = job.get("id")
    if not job_id:
        return False, "任务未创建"
    try:
        detail = client.get(f"/api/jobs/{job_id}").json()
    except Exception as err:  # noqa: BLE001
        return False, f"读取任务明细失败：{err}"

    tasks = detail.get("tasks", [])
    if not tasks:
        return False, "没有任务明细"

    bad = [t for t in tasks if t.get("status") != "succeeded"]
    if bad:
        reasons = "; ".join(f"{t['fileName']}({t.get('status')}: {t.get('error') or '无错误信息'})" for t in bad)
        return False, reasons
    return True, ""


def main() -> int:
    print("=" * 70)
    print("打包内核验证")
    print("=" * 70)

    if WORK.exists():
        import shutil

        shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)

    fixtures = make_fixtures()
    print(f"[信息] 已生成 {len(fixtures)} 个测试素材")

    core = PackagedCore()
    try:
        started = time.time()
        core.start()
        print(f"[信息] 打包内核启动耗时 {time.time() - started:.1f}s，端口 {core.port}")

        with core.client as client:
            print("\n== 1. 基础连通 ==")
            health = client.get("/api/health").json()
            check(health["phase"] == "ready", f"内核就绪（{health['python']}）")

            caps = client.get("/api/system/capabilities").json()
            engines = {e["id"]: e for e in caps["engines"]}
            check(engines["pdf.pymupdf"]["available"], "PyMuPDF（PDF 引擎）已打包")
            check(engines["image.pillow"]["available"], "Pillow（图像引擎）已打包")
            check(engines["ocr.rapidocr"]["available"], "本地 OCR 引擎已打包（含 ONNX 模型）")
            check(engines["pdf.pdf2docx"]["available"], "pdf2docx 已打包")
            check(engines["image.opencv"]["available"], "OpenCV 已打包（原生 DLL 完整）")
            # HEIC 支持特别容易漏：pillow-heif 只在运行时被注册、没有静态 import，
            # 不显式列进 hiddenimports 的话开发环境能用、装出来却报"不支持 HEIC"。
            check(
                engines["image.heif"]["available"],
                "HEIC/HEIF 支持已打包（iPhone 照片可读）",
            )

            print("\n== 2. 环境自检 ==")
            report = get_json(client, "/api/system/selfcheck")
            check(report["failCount"] == 0, f"自检无失败项（通过 {report['okCount']} / 降级 {report['warnCount']}）")

            print("\n== 3. 图片水印（Pillow + 中文字体）==")
            job = run_job(
                client,
                "image.watermark",
                [fixtures["photo"]],
                {"mode": "text", "text": "打包验证", "position": "center"},
                WORK / "wm-out",
            )
            check(job.get("status") == "succeeded", f"图片水印完成（{job.get('status')}）")
            check(len(list((WORK / "wm-out").glob("*.png"))) == 1, "产出水印图片")

            if fixtures.get("heic_ok"):
                heic_job = run_job(
                    client,
                    "image.watermark",
                    [WORK / "src" / "iphone.heic"],
                    {"mode": "text", "text": "HEIC", "position": "center"},
                    WORK / "heic-out",
                )
                # 必须检查**文件级**结果：格式不匹配时文件会被跳过，
                # 而任务状态仍是 succeeded，只看任务状态会假通过
                ok, reason = job_all_files_succeeded(client, heic_job)
                check(ok, "HEIC（iPhone 照片）处理成功", reason)

            print("\n== 4. 本地 OCR（ONNX Runtime + 模型文件）==")
            job = run_job(
                client,
                "image.ocr",
                [fixtures["text_image"]],
                {"engine": "local", "mode": "text", "output_format": "txt"},
                WORK / "ocr-out",
            )
            check(job.get("status") == "succeeded", f"OCR 完成（{job.get('status')}）")
            ocr_files = list((WORK / "ocr-out").glob("*.txt"))
            if ocr_files:
                text = ocr_files[0].read_text(encoding="utf-8").replace(" ", "")
                check("打包后内核验证" in text, "中文识别正确")
                check("Packed" in text, "英文识别正确")
            else:
                check(False, "产出文本文件")

            print("\n== 5. 图片转 Excel（OpenCV + openpyxl）==")
            job = run_job(
                client,
                "image.to_excel",
                [fixtures["table"]],
                {"engine": "local", "header_row": "auto"},
                WORK / "excel-out",
            )
            check(job.get("status") == "succeeded", f"转 Excel 完成（{job.get('status')}）")
            excel_files = list((WORK / "excel-out").glob("*.xlsx"))
            if excel_files:
                from openpyxl import load_workbook

                sheet = load_workbook(excel_files[0]).active
                check(sheet.max_column == 3, f"表格识别出 3 列（实际 {sheet.max_column}）")
            else:
                check(False, "产出 xlsx 文件")

            print("\n== 6. PDF 水印（PyMuPDF 原生库）==")
            job = run_job(
                client,
                "pdf.watermark",
                [fixtures["pdf"]],
                {"mode": "text", "text": "打包水印", "position": "center"},
                WORK / "pdf-out",
            )
            check(job.get("status") == "succeeded", f"PDF 水印完成（{job.get('status')}）")
            pdf_files = list((WORK / "pdf-out").glob("*.pdf"))
            if pdf_files:
                import pymupdf

                doc = pymupdf.open(str(pdf_files[0]))
                text = "".join(page.get_text() for page in doc)
                check("打包水印" in text, "水印写入成功")
                check("Packed test page" in text, "原文保留")
                doc.close()
            else:
                check(False, "产出 PDF 文件")

            print("\n== 7. PDF 转 Word（pdf2docx）==")
            job = run_job(
                client,
                "pdf.to_word",
                [fixtures["pdf"]],
                {"pages": "all"},
                WORK / "word-out",
            )
            check(job.get("status") == "succeeded", f"PDF 转 Word 完成（{job.get('status')}）")
            check(len(list((WORK / "word-out").glob("*.docx"))) == 1, "产出 docx 文件")

            print("\n== 8. 扫描件转可搜索 PDF（新增：不可见文字层）==")
            # 刻意用**纯图片**做源：有文字层的 PDF 会被原样复制过去，
            # 那样就测不到新的栅格化 + 叠加文字层这条路径了
            scan_pdf = fixtures["scan_pdf"]
            job = run_job(
                client,
                "pdf.searchable",
                [scan_pdf],
                {"engine": "local", "dpi": 150, "language": "zh"},
                WORK / "searchable-out",
            )
            ok, reason = job_all_files_succeeded(client, job)
            check(ok, "扫描件转可搜索 PDF 完成", reason)

            searchable = list((WORK / "searchable-out").glob("*.pdf"))
            if searchable:
                import pymupdf

                doc = pymupdf.open(str(searchable[0]))
                try:
                    text = "".join(page.get_text() for page in doc)
                    images = sum(len(page.get_images(full=True)) for page in doc)
                finally:
                    doc.close()
                # 可搜索＝文字能被取回；外观不变＝页面里仍有原始扫描图
                check("打包后内核验证" in text.replace(" ", ""), "文字层可被搜索/复制")
                check(images >= 1, f"扫描底图仍在（{images} 张）")
            else:
                check(False, "产出可搜索 PDF")

            print("\n== 9. 参数预设（新增：SQLite 表 + 新路由）==")
            created = client.post(
                "/api/presets",
                json={
                    "action": "image.watermark",
                    "name": "打包验证预设",
                    "params": {"mode": "text", "text": "预设水印", "opacity": 0.42},
                },
            )
            check(created.status_code == 200, f"保存预设（HTTP {created.status_code}）")

            listing = client.get("/api/presets", params={"action": "image.watermark"}).json()
            names = [p["name"] for p in listing["presets"]]
            check("打包验证预设" in names, "预设可被读回")
            if "打包验证预设" in names:
                preset = next(p for p in listing["presets"] if p["name"] == "打包验证预设")
                check(preset["params"].get("opacity") == 0.42, "预设参数原样往返")

            print("\n== 10. 多步骤流水线（新增：动作串联）==")
            # 两步且**跨类型**：图片 → 水印（同源）+ 图片 → 水印，
            # 顺便验证最后一步的扩展名被正确用作输出名
            job = run_job(
                client,
                "pipeline",
                [fixtures["photo"]],
                {
                    "__steps": [
                        {"action": "image.watermark", "params": {"mode": "text", "text": "第一步"}},
                        {"action": "image.watermark", "params": {"mode": "text", "text": "第二步"}},
                    ]
                },
                WORK / "pipeline-out",
            )
            ok, reason = job_all_files_succeeded(client, job)
            check(ok, "两步流水线完成", reason)

            pipeline_files = list((WORK / "pipeline-out").glob("*.png"))
            check(len(pipeline_files) == 1, "输出目录里只有最终产物（中间文件已清理）")
            if pipeline_files:
                check(
                    pipeline_files[0].read_bytes() != fixtures["photo"].read_bytes(),
                    "流水线确实改动了内容",
                )

            # 跨类型：文档 → PDF → 水印，产物必须是 .pdf 而不是 .docx
            docx_fixture = fixtures["docx"]
            job = run_job(
                client,
                "pipeline",
                [docx_fixture],
                {
                    "__steps": [
                        {"action": "doc.to_pdf"},
                        {"action": "pdf.watermark", "params": {"mode": "text", "text": "流水线水印"}},
                    ]
                },
                WORK / "pipeline-doc-out",
            )
            ok, reason = job_all_files_succeeded(client, job)
            check(ok, "跨类型流水线（docx → pdf → 水印）完成", reason)
            produced = list((WORK / "pipeline-doc-out").iterdir())
            check(
                len(produced) == 1 and produced[0].suffix == ".pdf",
                f"产物扩展名跟随最后一步（{[p.name for p in produced]}）",
            )

    finally:
        core.stop()

    _cli_checks()

    print("\n" + "=" * 70)
    if _failures:
        print(f"结果：{len(_failures)} 项失败")
        for item in _failures:
            print(f"  · {item}")
        return 1
    print("结果：全部通过 ✓  打包后的内核功能完整")
    return 0


def _cli_checks() -> bool:
    """验证**打包后的可执行文件**也能当命令行用。

    CLI 是新增能力，而且走的是 `docforge.cli` 这条惰性导入的代码路径 ——
    PyInstaller 的静态分析要是漏了这个模块，打包后执行 `docforge-core.exe run`
    会直接报 ModuleNotFoundError，而源码运行完全正常。这类问题只有真跑一遍
    打包产物才能发现，所以必须单独测。
    """
    print("\n== 11. 命令行（打包后的 exe 同样是 CLI）==")

    def run_cli(*argv: str, timeout: float = 180.0) -> subprocess.CompletedProcess[str]:
        env = {
            **dict(__import__("os").environ),
            "PYTHONIOENCODING": "utf-8",
            "DOCFORGE_DATA_DIR": str(WORK / "data"),
        }
        return subprocess.run(
            [str(EXE), *argv],
            cwd=str(EXE.parent),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

    listing = run_cli("actions")
    check(listing.returncode == 0, f"docforge-core actions（退出码 {listing.returncode}）")
    check("pipeline" in listing.stdout, "动作清单里含流水线")
    check("pdf.searchable" in listing.stdout, "动作清单里含扫描件转可搜索 PDF")

    check_cli = run_cli("selfcheck", "--json")
    check(check_cli.returncode == 0, f"docforge-core selfcheck（退出码 {check_cli.returncode}）")
    check('"items"' in check_cli.stdout, "自检返回结构化结果")

    version = run_cli("--version")
    check(version.returncode == 0, f"docforge-core --version（退出码 {version.returncode}）")
    check("文枢 DocForge" in version.stdout, "版本信息正确")

    # 真的跑一次流水线。注意输出目录要单独给，避免和上面的任务互相干扰。
    out_dir = WORK / "cli-pipeline-out"
    piped = run_cli(
        "pipeline",
        str(WORK / "src" / "照片.png"),
        "-o",
        str(out_dir),
        "--step",
        "image.watermark:text=命令行第一步",
        "--step",
        "image.watermark:text=命令行第二步",
    )
    check(piped.returncode == 0, f"docforge-core pipeline 执行（退出码 {piped.returncode}）")
    check(len(list(out_dir.glob("*.png"))) == 1, "命令行流水线产出最终结果")

    # 退出码约定：类型不匹配应当是 2，而不是 0（否则脚本无法判断成败）
    bad = run_cli("pipeline", str(WORK / "src" / "照片.png"), "--step", "pdf.watermark")
    check(bad.returncode == 2, f"类型不匹配时退出码为 2（实际 {bad.returncode}）")

    return True


if __name__ == "__main__":
    sys.exit(main())
