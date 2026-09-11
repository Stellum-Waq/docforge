"""端到端冒烟测试：起一个真实内核进程，走完整 HTTP 流程验证功能。

这不是单元测试（那在 core/tests 里），而是**贴近真实运行的集成验证**：
真的拉起 `python -m docforge`、真的握手、真的通过 HTTP 提交任务、真的检查产物。

用途：
  * 改完内核后快速确认没把链路弄坏
  * 交付前的手工验收
  * 排查"单元测试都过但应用里不工作"这类问题

用法：
    core/.venv/Scripts/python.exe tools/smoke_test.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
PY = CORE / ".venv" / "Scripts" / "python.exe"
WORK = REPO / ".cache" / "smoke"

# Windows 控制台默认用 cp936 解码，中文报告会变乱码。强制 UTF-8 输出。
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

READY_RE = re.compile(r"@@DOCFORGE_READY@@\s*(\{.*\})")

PASS = "  [通过]"
FAIL = "  [失败]"
INFO = "  [信息]"

_failures: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> bool:
    if condition:
        print(f"{PASS} {label}")
    else:
        print(f"{FAIL} {label}{(' — ' + detail) if detail else ''}")
        _failures.append(label)
    return condition


# --------------------------------------------------------------------------- #
# 测试素材                                                                      #
# --------------------------------------------------------------------------- #

def make_fixtures() -> dict[str, Path]:
    """生成覆盖关键边界的测试图。

    刻意包含这几类容易出问题的素材：
      * 带透明通道的 PNG（考验 JPEG 拍平）
      * 带 EXIF Orientation 的竖拍照片（考验水印方向）
      * 超宽图与超窄图（考验按短边缩放的智能适配）
    """
    src = WORK / "src"
    src.mkdir(parents=True, exist_ok=True)

    fixtures: dict[str, Path] = {}

    plain = src / "普通照片.png"
    Image.new("RGB", (1600, 1200), "#2A6FB0").save(plain)
    fixtures["plain"] = plain

    transparent = src / "带透明通道.png"
    Image.new("RGBA", (800, 600), (20, 180, 120, 110)).save(transparent)
    fixtures["transparent"] = transparent

    rotated = src / "手机竖拍.jpg"
    image = Image.new("RGB", (1200, 800), "#A05030")
    exif = image.getexif()
    exif[0x0112] = 6  # Rotate 90 CW
    image.save(rotated, exif=exif.tobytes())
    fixtures["rotated"] = rotated

    wide = src / "超宽横幅.png"
    Image.new("RGB", (2400, 300), "#334455").save(wide)
    fixtures["wide"] = wide

    tall = src / "超窄长图.png"
    Image.new("RGB", (300, 2400), "#556677").save(tall)
    fixtures["tall"] = tall

    broken = src / "损坏文件.png"
    broken.write_bytes(b"NOT A REAL PNG AT ALL")
    fixtures["broken"] = broken

    # OCR 专用素材
    text_image = src / "文档截图.png"
    canvas = Image.new("RGB", (1000, 380), "white")
    draw = ImageDraw.Draw(canvas)
    try:
        title_font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 38)
        body_font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 26)
    except OSError:
        title_font = body_font = ImageFont.load_default()
    draw.text((50, 40), "文枢 DocForge 文字识别", font=title_font, fill="black")
    draw.text((50, 120), "办公文件转换与处理工具", font=body_font, fill="black")
    draw.text((50, 170), "Invoice No. 2026-0911", font=body_font, fill="black")
    draw.text((50, 220), "合计金额：¥1,234.56", font=body_font, fill="black")
    canvas.save(text_image)
    fixtures["text_image"] = text_image

    table_image = src / "表格截图.png"
    sheet = Image.new("RGB", (900, 320), "white")
    pen = ImageDraw.Draw(sheet)
    cell_font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 28) if Path("C:/Windows/Fonts/msyh.ttc").is_file() else body_font
    grid_x = [30, 300, 570, 840]
    grid_y = [40, 130, 220, 310]
    for y in grid_y:
        pen.line([(grid_x[0], y), (grid_x[-1], y)], fill="black", width=3)
    for x in grid_x:
        pen.line([(x, grid_y[0]), (x, grid_y[-1])], fill="black", width=3)
    headers = ["姓名", "部门", "金额"]
    rows = [["张三", "技术部", "1200"], ["李四", "市场部", "980"]]
    for col, text in enumerate(headers):
        pen.text((grid_x[col] + 30, grid_y[0] + 30), text, font=cell_font, fill="black")
    for r, values in enumerate(rows, start=1):
        for col, text in enumerate(values):
            pen.text((grid_x[col] + 30, grid_y[r] + 30), text, font=cell_font, fill="black")
    sheet.save(table_image)
    fixtures["table_image"] = table_image

    # PDF 专用素材：两份多页 PDF（用于水印与合并）
    import pymupdf

    for name, pages, label in (("报告A.pdf", 4, "Report A"), ("报告B.pdf", 2, "Report B")):
        doc = pymupdf.open()
        for index in range(pages):
            page = doc.new_page(width=595, height=842)
            page.insert_text((60, 90), f"{label} - page {index + 1}", fontsize=18, fontname="helv")
            page.insert_text((60, 130), "Body content for smoke testing.", fontsize=11, fontname="helv")
        path = src / name
        doc.save(str(path))
        doc.close()
        fixtures[name.split(".")[0].lower().replace("报告", "pdf_")] = path

    return fixtures


# --------------------------------------------------------------------------- #
# 内核进程                                                                      #
# --------------------------------------------------------------------------- #

class Core:
    def __init__(self) -> None:
        self.proc: subprocess.Popen[str] | None = None
        self.port = 0
        self.token = ""

    def start(self) -> None:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env["DOCFORGE_DATA_DIR"] = str(WORK / "data")

        self.proc = subprocess.Popen(
            [str(PY), "-m", "docforge"],
            cwd=str(CORE),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        deadline = time.time() + 40
        assert self.proc.stdout is not None
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                if self.proc.poll() is not None:
                    err = self.proc.stderr.read() if self.proc.stderr else ""
                    raise RuntimeError(f"内核提前退出：\n{err}")
                continue
            match = READY_RE.search(line)
            if match:
                payload = json.loads(match.group(1))
                self.port, self.token = payload["port"], payload["token"]
                print(f"{INFO} 内核就绪 · 端口 {self.port}")
                return
        raise RuntimeError("内核未在 40s 内就绪")

    def stop(self) -> None:
        """停止内核。

        刻意**先等它自己优雅退出**再考虑强杀 —— 这与 Electron 的真实行为一致
        （Electron 会先 POST /api/shutdown，等 8 秒才 kill）。
        如果这里直接 terminate，Windows 会走 TerminateProcess，
        内核根本没机会回收 Office 进程，测试就会误报"泄漏"。
        """
        if not self.proc or self.proc.poll() is not None:
            return

        try:
            self.proc.wait(timeout=20)
            print(f"{INFO} 内核已优雅退出（返回码 {self.proc.returncode}）")
            return
        except subprocess.TimeoutExpired:
            print(f"{INFO} 内核未在 20s 内自行退出，转为强制结束")
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    @property
    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url=f"http://127.0.0.1:{self.port}",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=120.0,
        )


# --------------------------------------------------------------------------- #
# 验证步骤                                                                      #
# --------------------------------------------------------------------------- #

def verify_auth(core: Core) -> None:
    print("\n== 1. 鉴权 ==")
    with httpx.Client(base_url=f"http://127.0.0.1:{core.port}", timeout=10) as anon:
        check(anon.get("/api/health").status_code == 401, "无 Token 被拒绝 (401)")
        check(
            anon.get("/api/health", headers={"Authorization": "Bearer wrong"}).status_code == 403,
            "错误 Token 被拒绝 (403)",
        )
    with core.client as client:
        check(client.get("/api/health").status_code == 200, "正确 Token 可访问 (200)")


def verify_ocr(core: Core, fixtures: dict[str, Path]) -> None:
    print("\n== 8. OCR 双引擎 ==")
    text_image = fixtures["text_image"]

    with core.client as client:
        actions = client.get("/api/actions").json()["actions"]
        ids = {a["id"] for a in actions}
        check("image.ocr" in ids, "已注册 image.ocr 动作")
        check("image.to_excel" in ids, "已注册 image.to_excel 动作")

        engines = client.get("/api/ocr/engines").json()
        local = next((e for e in engines["engines"] if e["offline"]), None)
        cloud = next((e for e in engines["engines"] if not e["offline"]), None)
        check(local is not None and local["available"], f"本地离线引擎可用（{local['label'] if local else '—'}）")
        check(cloud is not None and not cloud["available"], "云端引擎在未配置密钥时正确报告不可用")

        # 试识别
        resp = client.post(
            "/api/preview/ocr-test",
            json={"filePath": str(text_image), "engine": "local", "mode": "text"},
        )
        check(resp.status_code == 200, "试识别接口可用", resp.text[:200] if resp.status_code != 200 else "")
        payload = resp.json()
        text = (payload.get("text") or "").replace(" ", "")
        check("文枢" in text, f"识别出中文标题（{payload.get('lineCount')} 行）")
        check("DocForge" in text, "识别出英文单词")
        check("2026-0911" in text, "识别出单据编号")
        check("1,234.56" in text, "识别出带千分位的金额")
        check(payload["engine"] == "ocr.rapidocr", f"实际使用的引擎：{payload['engine']}")

        # 云端不可用时应自动降级而非报错
        fallback = client.post(
            "/api/preview/ocr-test",
            json={"filePath": str(text_image), "engine": "cloud", "mode": "text"},
        )
        if fallback.status_code == 200:
            check(
                fallback.json()["engine"] == "ocr.rapidocr",
                "要求云端但云端不可用时自动降级到本地",
            )
        else:
            check(False, "请求云端时应降级而不是报错", fallback.text[:200])

        # 不存在的文件必须给出可读原因
        missing = client.post(
            "/api/preview/ocr-test",
            json={"filePath": str(Path("不存在的文件.png")), "engine": "local"},
        )
        check(missing.status_code == 400, "文件不存在时返回 400")


def verify_ocr_jobs(core: Core, fixtures: dict[str, Path]) -> None:
    print("\n== 9. OCR 批量任务 ==")
    out_dir = WORK / "ocr-out"

    with core.client as client:
        # 文字提取
        resp = client.post(
            "/api/jobs",
            json={
                "action": "image.ocr",
                "files": [str(fixtures["text_image"])],
                "outputDir": str(out_dir),
                "params": {"engine": "local", "mode": "text", "output_format": "txt"},
                "suffix": "_识别",
            },
        )
        check(resp.status_code == 201, "OCR 任务创建成功", resp.text[:200] if resp.status_code != 201 else "")
        job_id = resp.json()["job"]["id"]
        job = _wait_job(client, job_id)
        check(job["status"] == "succeeded", f"OCR 任务完成（{job['status']}）")

        produced = list(out_dir.glob("*_识别.txt"))
        check(len(produced) == 1, f"产出文本文件（{len(produced)}）")
        if produced:
            content = produced[0].read_text(encoding="utf-8")
            check("文枢" in content, "输出文本包含识别内容")

        # 图片转 Excel
        excel_dir = WORK / "excel-out"
        resp = client.post(
            "/api/jobs",
            json={
                "action": "image.to_excel",
                "files": [str(fixtures["table_image"])],
                "outputDir": str(excel_dir),
                "params": {"engine": "local", "header_row": "auto"},
            },
        )
        check(resp.status_code == 201, "转 Excel 任务创建成功")
        job_id = resp.json()["job"]["id"]
        job = _wait_job(client, job_id)
        check(job["status"] == "succeeded", f"转 Excel 任务完成（{job['status']}）")

        workbooks = list(excel_dir.glob("*.xlsx"))
        check(len(workbooks) == 1, "产出 xlsx 文件")
        if workbooks:
            try:
                from openpyxl import load_workbook

                sheet = load_workbook(workbooks[0]).active
                grid = [
                    [sheet.cell(row=r, column=c).value for c in range(1, sheet.max_column + 1)]
                    for r in range(1, sheet.max_row + 1)
                ]
                flat = ["".join(str(v) for v in row if v is not None) for row in grid]
                check(sheet.max_column == 3, f"表格识别出 3 列（实际 {sheet.max_column}）")
                check(sheet.max_row == 3, f"表格识别出 3 行（实际 {sheet.max_row}）")
                check(any("姓名" in row for row in flat), f"表头识别正确：{flat[0] if flat else ''}")
                check(any("张三" in row for row in flat), "数据行识别正确")
            except Exception as err:  # noqa: BLE001
                check(False, "读取产出的 xlsx", str(err))


def verify_settings(core: Core) -> None:
    print("\n== 10. 设置与密钥安全 ==")
    with core.client as client:
        data = client.get("/api/settings").json()
        check("deepseekApiKeyMasked" in data, "设置接口返回密钥掩码字段")
        check("deepseek_api_key" not in data, "设置接口**不返回**密钥明文字段")
        check(data["hasApiKey"] is False, "当前未配置密钥")
        check(isinstance(data.get("encryptionAvailable"), bool), "返回加密可用性标志")

        updated = client.put(
            "/api/settings",
            json={"deepseekApiKey": "sk-test-key-for-smoke-1234567890", "cloudOcrEnabled": True},
        ).json()
        check(updated["hasApiKey"] is True, "保存密钥后 hasApiKey 为真")
        check(
            "sk-test-key-for-smoke-1234567890" not in json.dumps(updated, ensure_ascii=False),
            "保存后的响应中**不含明文密钥**",
        )
        check("*" in updated["deepseekApiKeyMasked"] or "•" in updated["deepseekApiKeyMasked"], "密钥以掩码形式返回")

        # 落盘文件里也不能有明文
        secrets_file = WORK / "data" / "secrets.dat"
        if secrets_file.is_file():
            raw = secrets_file.read_text(encoding="utf-8", errors="replace")
            check(
                "sk-test-key-for-smoke" not in raw,
                "磁盘上的配置文件**不含明文密钥**（已加密）",
            )
        else:
            check(False, "密钥配置文件已生成")

        # 空字符串表示不修改，避免误清空
        after = client.put("/api/settings", json={"deepseekApiKey": ""}).json()
        check(after["hasApiKey"] is True, "提交空密钥不会清空已有密钥")

        cleared = client.put("/api/settings", json={"clearApiKey": True}).json()
        check(cleared["hasApiKey"] is False, "显式清除密钥生效")

        # 清除密钥**不应该**连带关掉「启用云端」开关 —— 那是用户的偏好设置，
        # 保留它意味着重新填入密钥后自动恢复云端模式，不必再点一次。
        # 真正要保证的是：没有密钥时云端引擎必须报告不可用。
        engines = client.get("/api/ocr/engines").json()
        cloud = next((e for e in engines["engines"] if not e["offline"]), None)
        check(
            cloud is not None and cloud["available"] is False,
            "清除密钥后云端引擎报告不可用",
        )
        check(
            bool(cloud and cloud.get("reason")),
            f"并给出可读原因：{cloud.get('reason') if cloud else '—'}",
        )

        usage = client.get("/api/ocr/usage").json()
        check("usage" in usage and "cache" in usage, "用量与缓存统计接口可用")

        cached = client.delete("/api/ocr/cache").json()
        check(cached.get("ok") is True, "清空缓存接口可用")


def _wait_job(client: httpx.Client, job_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    job: dict = {}
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()["job"]
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.25)
    return job


def verify_pdf_watermark(core: Core, fixtures: dict[str, Path]) -> None:
    print("\n== 11. PDF 水印 ==")
    import pymupdf

    source = fixtures["pdf_a"]

    with core.client as client:
        actions = {a["id"]: a for a in client.get("/api/actions").json()["actions"]}
        check("pdf.watermark" in actions, "已注册 pdf.watermark 动作")

        # 元信息
        meta = client.post("/api/preview/pdf-meta", json={"filePath": str(source)}).json()
        check(meta["pageCount"] == 4, f"读取页数正确（{meta['pageCount']}）")
        check(len(meta["pages"]) == 4, "返回每页尺寸信息")

        # 预览：与正式输出同源
        preview = client.post(
            "/api/preview/pdf-watermark",
            json={
                "filePath": str(source),
                "page": 2,
                "params": {"mode": "text", "text": "内部机密", "position": "center", "rotation": -30},
                "maxWidth": 700,
            },
        )
        check(preview.status_code == 200, "PDF 水印预览渲染成功", preview.text[:200] if preview.status_code != 200 else "")
        check(preview.headers.get("content-type", "").startswith("image/png"), "预览返回 PNG")
        check(len(preview.content) > 3000, f"预览图非空（{len(preview.content)} 字节）")

        bad = client.post(
            "/api/preview/pdf-watermark",
            json={"filePath": str(source), "page": 1, "params": {"mode": "text", "text": "   "}},
        )
        check(bad.status_code == 400, "非法参数返回 400 而非 500")

        # 正式任务
        out_dir = WORK / "pdf-out"
        resp = client.post(
            "/api/jobs",
            json={
                "action": "pdf.watermark",
                "files": [str(source)],
                "outputDir": str(out_dir),
                "params": {
                    "mode": "text",
                    "text": "第{page}页",
                    "unique_per_file": True,
                    "position": "center",
                    "opacity": 0.4,
                },
                "suffix": "_已加水印",
            },
        )
        check(resp.status_code == 201, "PDF 水印任务创建成功")
        job_id = resp.json()["job"]["id"]
        job = _wait_job(client, job_id)
        check(job["status"] == "succeeded", f"PDF 水印任务完成（{job['status']}）")

        produced = list(out_dir.glob("*_已加水印.pdf"))
        check(len(produced) == 1, f"产出 PDF（{len(produced)}）")

        if produced:
            doc = pymupdf.open(str(produced[0]))
            try:
                check(doc.page_count == 4, f"页数保持 4（实际 {doc.page_count}）")
                text = doc[0].get_text()
                # 水印是矢量文字，因此可以被提取 —— 这同时证明了它可被检索
                check("第1页" in text, "水印文字是矢量的、可被提取")
                check("Report A" in text, "原文内容未被破坏")
                # 逐页不同的模板变量
                check("第3页" in doc[2].get_text().replace(" ", ""), "模板变量逐页生效")
            finally:
                doc.close()


def verify_pdf_toolbox(core: Core, fixtures: dict[str, Path]) -> None:
    print("\n== 12. PDF 工具箱 ==")
    import pymupdf

    with core.client as client:
        actions = {a["id"] for a in client.get("/api/actions").json()["actions"]}
        for action_id in ("pdf.merge", "pdf.split", "pdf.rotate", "pdf.compress", "pdf.extract_pages", "pdf.to_images", "pdf.info"):
            check(action_id in actions, f"已注册 {action_id}")

        # --- 聚合动作：合并 ---
        merge_dir = WORK / "merge-out"
        resp = client.post(
            "/api/jobs",
            json={
                "action": "pdf.merge",
                "files": [str(fixtures["pdf_a"]), str(fixtures["pdf_b"])],
                "outputDir": str(merge_dir),
                "params": {"add_bookmarks": True},
            },
        )
        check(resp.status_code == 201, "合并任务创建成功")
        created = resp.json()
        check(len(created["tasks"]) == 1, f"聚合动作只创建 1 个任务（实际 {len(created['tasks'])}）")

        job = _wait_job(client, created["job"]["id"])
        check(job["status"] == "succeeded", f"合并完成（{job['status']}）")

        merged = list(merge_dir.glob("*.pdf"))
        check(len(merged) == 1, f"只产出 1 个文件（实际 {[p.name for p in merged]}）")
        if merged:
            doc = pymupdf.open(str(merged[0]))
            try:
                check(doc.page_count == 6, f"合并后共 6 页（实际 {doc.page_count}）")
                check("Report A" in doc[0].get_text(), "顺序正确：A 在前")
                check("Report B" in doc[4].get_text(), "顺序正确：B 在后")
                check(len(doc.get_toc()) == 2, "生成了书签")
            finally:
                doc.close()

        # --- 单文件动作 ---
        cases = [
            ("pdf.split", {"split_mode": "every", "pages_per_file": 2}, ".pdf", "拆分"),
            ("pdf.rotate", {"angle": "90", "pages": "1"}, ".pdf", "旋转"),
            ("pdf.extract_pages", {"pages": "2,3"}, ".pdf", "提取页"),
            ("pdf.info", {}, ".json", "文档信息"),
        ]
        for action_id, params, ext, label in cases:
            case_dir = WORK / f"tool-{action_id.replace('.', '-')}"
            resp = client.post(
                "/api/jobs",
                json={
                    "action": action_id,
                    "files": [str(fixtures["pdf_a"])],
                    "outputDir": str(case_dir),
                    "params": params,
                },
            )
            if resp.status_code != 201:
                check(False, f"{label} 任务创建", resp.text[:200])
                continue
            job = _wait_job(client, resp.json()["job"]["id"])
            check(job["status"] == "succeeded", f"{label} 完成（{job['status']}）")

        # 拆分产物
        parts = list((WORK / "tool-pdf-split").glob("**/*.pdf"))
        check(len(parts) == 2, f"4 页按每 2 页拆分产出 2 个文件（实际 {len(parts)}）")

        # 提取页产物
        extracted = list((WORK / "tool-pdf-extract_pages").glob("*.pdf"))
        if extracted:
            doc = pymupdf.open(str(extracted[0]))
            try:
                check(doc.page_count == 2, f"提取页产出 2 页（实际 {doc.page_count}）")
            finally:
                doc.close()

        # 旋转产物
        rotated = list((WORK / "tool-pdf-rotate").glob("*.pdf"))
        if rotated:
            doc = pymupdf.open(str(rotated[0]))
            try:
                check(doc[0].rotation == 90, f"第 1 页旋转 90°（实际 {doc[0].rotation}）")
                check(doc[1].rotation == 0, "未选中的页面不受影响")
            finally:
                doc.close()

        # 压缩产物
        compress_dir = WORK / "tool-compress"
        resp = client.post(
            "/api/jobs",
            json={
                "action": "pdf.compress",
                "files": [str(fixtures["pdf_a"])],
                "outputDir": str(compress_dir),
                "params": {},
            },
        )
        job = _wait_job(client, resp.json()["job"]["id"])
        check(job["status"] == "succeeded", f"压缩完成（{job['status']}）")

        # 转图片
        images_dir = WORK / "tool-images"
        resp = client.post(
            "/api/jobs",
            json={
                "action": "pdf.to_images",
                "files": [str(fixtures["pdf_b"])],
                "outputDir": str(images_dir),
                "params": {"image_format": "png", "dpi": 72},
            },
        )
        job = _wait_job(client, resp.json()["job"]["id"])
        check(job["status"] == "succeeded", f"转图片完成（{job['status']}）")
        pngs = list(images_dir.glob("**/*.png"))
        check(len(pngs) == 2, f"2 页导出 2 张图片（实际 {len(pngs)}）")


def verify_doc_convert(core: Core, fixtures: dict[str, Path], baseline: list[set[int]]) -> None:
    print("\n== 13. 文档转换（PDF ↔ Word）==")
    import psutil
    import pymupdf

    def office_pids() -> set[int]:
        """当前所有 Office 进程。

        **必须区分"我们拉起的"与"用户自己开着的"**：本机就可能有一个用户
        自己的 Excel 在跑（实测就有），它绝不能被算作泄漏，更不能被碰。
        因此验收用的是"相对基线的增量"。
        """
        return {
            p.pid
            for p in psutil.process_iter(["name"])
            if (p.info.get("name") or "").lower() in ("winword.exe", "excel.exe", "powerpnt.exe")
        }

    with core.client as client:
        actions = {a["id"]: a for a in client.get("/api/actions").json()["actions"]}
        check("doc.to_pdf" in actions, "已注册 doc.to_pdf 动作")
        check("pdf.to_word" in actions, "已注册 pdf.to_word 动作")
        check(
            actions["doc.to_pdf"]["aggregate"] is False,
            "单文件动作的 aggregate 元数据为 false",
        )
        check(
            actions["pdf.merge"]["aggregate"] is True,
            "聚合动作的 aggregate 元数据为 true（界面据此改变交互）",
        )

        # 造一份带中文与表格的 Word
        try:
            from docx import Document

            source = WORK / "src" / "季度报告.docx"
            document = Document()
            document.add_heading("季度工作报告", level=1)
            document.add_paragraph("本季度各项指标均按计划推进，办公文件转换工具已完成核心功能。")
            table = document.add_table(rows=2, cols=3)
            for col, text in enumerate(("项目", "计划", "完成")):
                table.cell(0, col).text = text
            for col, text in enumerate(("文档转换", "100", "100")):
                table.cell(1, col).text = text
            document.save(str(source))
        except Exception as err:  # noqa: BLE001
            check(False, "生成测试 Word 文档", str(err))
            return

        # --- Word → PDF ---
        out_dir = WORK / "doc-out"
        baseline[0] = office_pids()

        resp = client.post(
            "/api/jobs",
            json={
                "action": "doc.to_pdf",
                "files": [str(source)],
                "outputDir": str(out_dir),
                "params": {"engine": "com", "bookmarks": True},
                "suffix": "_转换",
            },
        )
        check(resp.status_code == 201, "文档转 PDF 任务创建成功")
        job = _wait_job(client, resp.json()["job"]["id"], timeout=300)
        check(job["status"] == "succeeded", f"转换完成（{job['status']}）")

        produced = list(out_dir.glob("*_转换.pdf"))
        check(len(produced) == 1, f"产出 PDF（{len(produced)}）")

        if produced:
            doc = pymupdf.open(str(produced[0]))
            try:
                text = "".join(page.get_text() for page in doc)
                check("季度工作报告" in text, "标题被还原为可选中文字（说明不是图片）")
                check("办公文件转换工具" in text, "正文被还原")
                check("文档转换" in text, "表格内容被还原")
                check(len(doc.get_toc()) > 0, "生成了 PDF 书签目录")
            finally:
                doc.close()

        # COM 进程回收的验收点放在 main()（内核退出之后）。
        # 这里**不能**断言"转换后没有 Word 进程"：工作线程刻意复用同一个实例
        # （每次重启要 1–3 秒），转换刚结束时它本来就该活着。

        # --- PDF → Word ---
        back_dir = WORK / "word-out"
        # 用前面 Word→PDF 的产物做往返测试：如果 LibreOffice/COM 都不可用，
        # 就退回到 PDF 工具箱章节生成的 PDF，保证这一节永远有得测。
        back_source = produced[0] if produced else fixtures["pdf_a"]

        resp = client.post(
            "/api/jobs",
            json={
                "action": "pdf.to_word",
                "files": [str(back_source)],
                "outputDir": str(back_dir),
                "params": {"pages": "all"},
            },
        )
        check(resp.status_code == 201, "PDF 转 Word 任务创建成功")
        job = _wait_job(client, resp.json()["job"]["id"], timeout=300)
        check(job["status"] == "succeeded", f"反向转换完成（{job['status']}）")

        docx_files = list(back_dir.glob("*.docx"))
        check(len(docx_files) == 1, f"产出 Word 文档（{len(docx_files)}）")

        if docx_files:
            from docx import Document

            document = Document(str(docx_files[0]))
            back_text = "\n".join(p.text for p in document.paragraphs)
            check("季度工作报告" in back_text, "PDF 内容被还原回 Word")
            check("办公文件转换工具" in back_text, "正文内容完整往返")

        # --- 引擎信息 ---
        engines_resp = client.get("/api/system/capabilities").json()
        office_engines = {
            e["id"]: e for e in engines_resp["engines"] if e["domain"] == "office"
        }
        available = [e["label"] for e in office_engines.values() if e["available"]]
        check(len(available) >= 2, f"至少有 2 个可用转换引擎：{available}")

        # 优雅退出接口：Electron 关停内核前会调它，让内核有机会回收 Office 进程。
        # 注意这里**不能**在转换后立刻断言"没有 Word 进程" —— 工作线程会刻意
        # 复用同一个 Word 实例（每次重启要 1–3 秒），进程本来就应该活着。
        # 真正的验收点是"内核退出后不留残留"，放在 main() 里检查。
        shutdown_resp = client.post("/api/shutdown")
        check(shutdown_resp.status_code == 200, "优雅退出接口可用")


def verify_selfcheck(core: Core) -> None:
    print("\n== 15. 环境自检 ==")
    with core.client as client:
        resp = client.get("/api/system/selfcheck")
        check(resp.status_code == 200, "自检接口可用", resp.text[:200] if resp.status_code != 200 else "")
        if resp.status_code != 200:
            return

        report = resp.json()
        check("items" in report and len(report["items"]) > 5, f"返回 {len(report.get('items', []))} 项检查")
        check(
            report["okCount"] + report["warnCount"] + report["failCount"] == len(report["items"]),
            "计数与明细一致",
        )
        check(report["ready"] is True, "关键项无失败（ready=true）")

        # 每项都必须有可读说明；降级/失败项还必须有"影响"说明，
        # 否则用户看到"不可用"却不知道会怎么样
        missing_detail = [i["label"] for i in report["items"] if not i.get("detail")]
        check(not missing_detail, f"每项都有说明（缺说明：{missing_detail}）")

        missing_impact = [
            i["label"] for i in report["items"] if i["status"] != "ok" and not i.get("impact")
        ]
        check(not missing_impact, f"降级/失败项都说明了影响（缺影响：{missing_impact}）")

        # 关键能力应当被识别为可用
        by_id = {item["id"]: item for item in report["items"]}
        for key, label in (
            ("core.runtime", "内核"),
            ("storage.output_dir", "输出目录"),
            ("pdf.pymupdf", "PDF 引擎"),
            ("image.pillow", "图像引擎"),
        ):
            item = by_id.get(key)
            check(item is not None and item["status"] == "ok", f"{label} 自检通过")


def verify_actions(core: Core) -> str:
    print("\n== 2. 动作注册表 ==")
    with core.client as client:
        resp = client.get("/api/actions")
        resp.raise_for_status()
        actions = resp.json()["actions"]

    ids = [a["id"] for a in actions]
    check("image.watermark" in ids, f"已注册动作：{ids}")
    action = next(a for a in actions if a["id"] == "image.watermark")
    check(len(action["paramsSchema"].get("properties", {})) >= 15, "参数 Schema 完整（≥15 个参数）")
    return "image.watermark"


def verify_preview(core: Core, sample: Path) -> None:
    print("\n== 3. 实时预览 ==")
    with core.client as client:
        resp = client.post(
            "/api/preview/image-watermark",
            json={
                "filePath": str(sample),
                "params": {"mode": "text", "text": "预览测试", "position": "center"},
                "maxWidth": 480,
            },
        )
        check(resp.status_code == 200, "预览渲染成功", resp.text[:200] if resp.status_code != 200 else "")
        check(resp.headers.get("content-type", "").startswith("image/png"), "返回 PNG")
        data = resp.content
        check(len(data) > 2000, f"预览图非空（{len(data)} 字节）")

        out = WORK / "preview.png"
        out.write_bytes(data)
        with Image.open(out) as img:
            check(img.width <= 480, f"预览被缩放到指定宽度（{img.width}px）")

        # 参数非法时必须给出可读原因，而不是 500
        bad = client.post(
            "/api/preview/image-watermark",
            json={"filePath": str(sample), "params": {"mode": "text", "text": "   "}},
        )
        check(bad.status_code == 400, "非法参数返回 400 而非 500")
        check("水印文字为空" in bad.text, "非法参数给出可读原因")


def verify_job(core: Core, action: str, fixtures: dict[str, Path]) -> str:
    print("\n== 4. 批量任务 ==")
    out_dir = WORK / "out"
    # 只挑水印动作真正接受的图片：素材集里还有 PDF、表格图等，它们会被正确跳过，
    # 因此期望值必须按"被接受的类型"来算，而不是按素材总数。
    image_exts = {"jpg", "jpeg", "png", "bmp", "webp", "tif", "tiff"}
    files = [str(p) for p in fixtures.values() if p.suffix.lstrip(".").lower() in image_exts]

    with core.client as client:
        # 先订阅事件流，确认能收到事件
        resp = client.post(
            "/api/jobs",
            json={
                "action": action,
                "files": files,
                "outputDir": str(out_dir),
                "params": {
                    "mode": "text",
                    "text": "{stem}",
                    "unique_per_file": True,
                    "position": "bottom-right",
                    "opacity": 0.6,
                    "output_format": "jpg",
                    "quality": 85,
                },
                "concurrency": 3,
                "suffix": "_水印",
            },
        )
        check(resp.status_code == 201, "任务创建成功", resp.text[:300] if resp.status_code != 201 else "")
        if resp.status_code != 201:
            return ""
        job_id = resp.json()["job"]["id"]

        # 轮询直到结束
        deadline = time.time() + 90
        job = {}
        while time.time() < deadline:
            job = client.get(f"/api/jobs/{job_id}").json()["job"]
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.25)

    check(job.get("status") == "succeeded", f"任务状态为 succeeded（实际 {job.get('status')}）")
    check(job.get("progress") == 100.0, f"进度达到 100（实际 {job.get('progress')}）")
    check(job.get("totalTasks") == len(files), f"任务总数 {job.get('totalTasks')} == 文件数 {len(files)}")
    check(job.get("failedTasks") == 1, f"仅损坏文件失败（failedTasks={job.get('failedTasks')}）")

    produced = sorted(out_dir.glob("*.jpg"))
    # 6 个素材里损坏的那个必然失败，其余 5 个都应产出
    expected_ok = len(files) - 1
    check(len(produced) == expected_ok, f"产出 {expected_ok} 个文件（实际 {len(produced)}）")

    names = {p.name for p in produced}
    check("普通照片_水印.jpg" in names, "输出文件名带后缀")
    check("带透明通道_水印.jpg" in names, "透明 PNG → JPEG 成功拍平")

    # 逐个检查产物有效性
    for path in produced:
        with Image.open(path) as img:
            img.verify()
    check(True, "全部产物可被 PIL 正常打开（未损坏）")

    # EXIF 方向：1200x800 带 Orientation=6 → 应为 800x1200
    rotated_out = out_dir / "手机竖拍_水印.jpg"
    if rotated_out.is_file():
        with Image.open(rotated_out) as img:
            check(img.size == (800, 1200), f"手机竖拍已按 EXIF 摆正（{img.size}）")
            check(img.getexif().get(0x0112) in (None, 1), "输出已清除 Orientation 标记")

    # 每张唯一水印：不同文件的文字内容应不同 → 像素必然不同
    if len(produced) >= 2:
        a = produced[0].read_bytes()
        b = produced[1].read_bytes()
        check(a != b, "「每张唯一水印」生效（不同文件输出不同）")

    return job_id


def verify_task_detail(core: Core, job_id: str) -> None:
    print("\n== 5. 任务明细与失败归因 ==")
    with core.client as client:
        detail = client.get(f"/api/jobs/{job_id}").json()
        tasks = detail["tasks"]

    failed = [t for t in tasks if t["status"] == "failed"]
    check(len(failed) == 1, f"恰好 1 个失败任务（实际 {len(failed)}）")
    if failed:
        check("无法打开图片" in (failed[0]["error"] or ""), f"失败原因可读：{failed[0]['error']}")
        check(failed[0]["fileName"] == "损坏文件.png", "失败被正确归因到具体文件")

    succeeded = [t for t in tasks if t["status"] == "succeeded"]
    check(all(t["durationMs"] is not None for t in succeeded), "成功任务都记录了耗时")
    check(all(t["outputPath"] for t in succeeded), "成功任务都记录了输出路径")


def verify_history(core: Core, job_id: str) -> None:
    print("\n== 6. 历史落盘 ==")
    with core.client as client:
        history = client.get("/api/jobs/history/list").json()
        ids = [j["id"] for j in history["jobs"]]
        check(job_id in ids, "任务已写入历史")

        tasks = client.get(f"/api/jobs/history/{job_id}/tasks").json()["tasks"]
        check(len(tasks) > 0, f"历史任务明细可查（{len(tasks)} 条）")


def verify_conflict_policy(core: Core, action: str, sample: Path) -> None:
    print("\n== 7. 重名策略与目录结构 ==")
    out_dir = WORK / "conflict"
    with core.client as client:
        for _ in range(2):
            resp = client.post(
                "/api/jobs",
                json={
                    "action": action,
                    "files": [str(sample)],
                    "outputDir": str(out_dir),
                    "params": {"mode": "text", "text": "A"},
                    "conflictPolicy": "rename",
                },
            )
            job_id = resp.json()["job"]["id"]
            deadline = time.time() + 60
            while time.time() < deadline:
                job = client.get(f"/api/jobs/{job_id}").json()["job"]
                if job["status"] in {"succeeded", "failed", "cancelled"}:
                    break
                time.sleep(0.2)

    names = sorted(p.name for p in out_dir.glob("*.png"))
    check(len(names) == 2, f"两次运行产出两个文件（{names}）")
    check("普通照片 (2).png" in names, "重名按 Windows 习惯追加 (2)")


# --------------------------------------------------------------------------- #

def main() -> int:
    print("=" * 68)
    print("文枢 DocForge · 端到端冒烟测试")
    print("=" * 68)

    if not PY.is_file():
        print(f"找不到虚拟环境 Python：{PY}")
        return 1

    if WORK.exists():
        import shutil

        shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)

    fixtures = make_fixtures()
    print(f"{INFO} 已生成 {len(fixtures)} 个测试素材（含损坏文件、带 EXIF 的手机照片、透明 PNG）")

    core = Core()
    # 供后续检查"内核退出后有没有留下 Office 残留进程"
    word_baseline: list[set[int]] = [set()]
    try:
        core.start()
        verify_auth(core)
        action = verify_actions(core)
        verify_preview(core, fixtures["plain"])
        job_id = verify_job(core, action, fixtures)
        if job_id:
            verify_task_detail(core, job_id)
            verify_history(core, job_id)
        verify_conflict_policy(core, action, fixtures["plain"])
        verify_ocr(core, fixtures)
        verify_ocr_jobs(core, fixtures)
        verify_settings(core)
        verify_pdf_watermark(core, fixtures)
        verify_pdf_toolbox(core, fixtures)
        verify_doc_convert(core, fixtures, word_baseline)
        verify_selfcheck(core)
    finally:
        core.stop()

    # ---- 内核退出后：不能留下无主的 Office 进程 ----
    # 这是 COM 自动化最容易留下的后遗症，也是 Electron 关闭应用时真实会走到的路径。
    print("\n== 16. 进程清理 ==")
    import psutil

    time.sleep(2.0)
    remaining = {
        p.pid
        for p in psutil.process_iter(["name"])
        if (p.info.get("name") or "").lower() in ("winword.exe", "excel.exe", "powerpnt.exe")
    } - word_baseline[0]
    if remaining:
        details = []
        for pid in remaining:
            try:
                process = psutil.Process(pid)
                details.append(f"{process.name()}(PID {pid})")
            except psutil.Error:
                details.append(f"PID {pid}")
        check(False, "内核退出后无残留 Office 进程", f"残留 {', '.join(details)}")
    else:
        check(True, "内核退出后无残留 Office 进程")

    print("\n" + "=" * 68)
    if _failures:
        print(f"结果：{len(_failures)} 项失败")
        for item in _failures:
            print(f"  · {item}")
        return 1
    print("结果：全部通过 ✓")
    print(f"产物目录：{WORK}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
