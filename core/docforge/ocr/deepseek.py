"""DeepSeek Vision 云端 OCR 引擎（模型 ``deepseek-flash``）。

## 已核实的接口约束（照着官方文档写，不是猜的）

| 项目 | 值 |
|---|---|
| 模型 | ``deepseek-flash``（**唯一**支持视觉的模型；``deepseek-v4-pro`` 不支持） |
| 端点 | ``POST https://api.deepseek.com/chat/completions``（OpenAI 兼容） |
| 图片传法 | Base64 ``data:`` URL / 公网 URL / Files API ``file_id`` |
| 支持格式 | JPEG / PNG / GIF / WebP（按**内容**嗅探，不看扩展名） |
| 请求体上限 | 48 MiB；单图 base64 上限 32 MiB |
| 缩放行为 | 推理前缩放到"约 1300×1300 等效像素"，**单图上限 1024 token** |
| JSON 模式 | ``response_format={"type":"json_object"}``，且提示词里必须出现 "json" |
| 图片位置 | 只允许出现在 ``user`` 消息里 |

## 缩放行为带来的关键后果

正因为会给图片"缩到 1300×1300 等效"，整张密集小字文档直接送进去会糊掉。
所以本引擎的核心不是"调接口"，而是 :mod:`docforge.ocr.tiling` 里的自适应切片：
先判断字号够不够，不够就切块分别识别再按坐标拼回。

## 成本控制

* 结果缓存：``文件内容哈希 + 提示词版本 + 参数`` 作为键，重复文件零计费
* 切片前先算：字够大就整图一次调用，不做无谓的多图请求
* 如实累计 token 用量，供界面展示真实花费
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageOps

from ..security.secrets import get_secret
from ..storage.cache import get_sync_cache
from .base import (
    OcrEngine,
    OcrError,
    OcrOptions,
    OcrResult,
    OcrUnavailable,
    ProgressCallback,
    TextBlock,
)
from .prompts import JSON_MODES, PROMPT_VERSION, build_prompt
from .table import merge_table_fragments
from .tiling import (
    GLYPH_FROM_BOX_RATIO,
    MIN_RELIABLE_GLYPH_PX,
    UNKNOWN_LINE_RATIO,
    estimate_line_height,
    merge_tile_blocks,
    model_scale,
    plan_tiles,
    sanity_line_height,
    sort_reading_order,
)

API_BASE = "https://api.deepseek.com"
CHAT_ENDPOINT = f"{API_BASE}/chat/completions"
MODEL = "deepseek-flash"

#: base64 单图上限 32 MiB，但请求体总上限 48 MiB。
#: 这里取更保守的值，避免多图请求叠加后超限。
MAX_IMAGE_BYTES = 8 * 1024 * 1024
#: 转 JPEG 时的质量。95 对文字几乎无损，同时体积远小于 PNG。
JPEG_QUALITY = 95

#: 单个请求的重试次数与退避基数
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.2

#: 输出预算的绝对上限。被截断时会一路翻倍到这个值。
#:
#: 预算是**天花板而不是花费** —— 模型答完就停，实测把上限从 16384 提到 32768，
#: 实际输出仍然只有约 12900 tokens，费用没有变化。所以这个上限尽管给足。
ABSOLUTE_MAX_TOKENS = 65536

#: 全局云端并发上限。官方并发限制很高（flash 为 2500），
#: 这里限制的是**本机**并发，避免用户一次拖入上千张图时把带宽打满。
CLOUD_CONCURRENCY = 4
#: 单张图片内部切片的并发数
TILE_CONCURRENCY = 3

_cloud_semaphore = threading.Semaphore(CLOUD_CONCURRENCY)
_client_lock = threading.Lock()
_client: httpx.Client | None = None

#: 单价（美元 / 1M tokens）。取保守的峰值价，宁可高估不高估。
PRICE_INPUT_PER_M = 0.30
PRICE_OUTPUT_PER_M = 1.20
#: 单图 token 上限，用于成本预估
MAX_IMAGE_TOKENS = 1024


def _get_client() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                base_url=API_BASE,
                timeout=httpx.Timeout(connect=15.0, read=180.0, write=120.0, pool=15.0),
                limits=httpx.Limits(max_connections=CLOUD_CONCURRENCY * 2, max_keepalive_connections=8),
            )
        return _client


def _encode_image(image: Image.Image) -> tuple[bytes, str]:
    """把 PIL 图片编码成适合上传的字节流。

    优先 PNG（无损，细字更清晰）；超过体积阈值时改用高质量 JPEG。
    直接无脑用 PNG 的话，一张 4000×3000 的扫描件可能到十几 MB，
    既慢又逼近 32 MiB 上限。
    """
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    data = buffer.getvalue()

    if len(data) <= MAX_IMAGE_BYTES:
        return data, "image/png"

    buffer = io.BytesIO()
    rgb = image.convert("RGB") if image.mode not in ("RGB", "L") else image
    rgb.save(buffer, format="JPEG", quality=JPEG_QUALITY, subsampling=0, optimize=True)
    return buffer.getvalue(), "image/jpeg"


def _content_hash(path: Path) -> str:
    """文件内容哈希。用内容而非路径，这样改名/移动后缓存依然命中。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_key(path: Path, options: OcrOptions, extra: str = "") -> str:
    parts = [
        "deepseek-vision",
        PROMPT_VERSION,
        options.mode,
        options.detail,
        options.tiling,
        options.language,
        str(options.max_tokens),
        hashlib.sha256((options.prompt_override or "").encode("utf-8")).hexdigest()[:16],
        extra,
        _content_hash(path),
    ]
    return "ocr:" + ":".join(parts)


class DeepSeekVisionEngine(OcrEngine):
    id = "ocr.deepseek.vision"
    label = "DeepSeek Vision (云端)"
    offline = False
    fidelity = 95

    #: 累计用量（进程级）。界面用它显示本次会话花了多少。
    _usage_lock = threading.Lock()
    _total_prompt_tokens = 0
    _total_completion_tokens = 0
    _total_calls = 0

    def availability(self) -> tuple[bool, str | None]:
        if not get_secret("deepseek_api_key"):
            return False, "尚未配置 DeepSeek API Key（在「设置 → OCR 引擎」中填写）"
        return True, None

    # ------------------------------------------------------------------ #
    # 用量统计                                                           #
    # ------------------------------------------------------------------ #

    @classmethod
    def usage_snapshot(cls) -> dict[str, Any]:
        with cls._usage_lock:
            cost = (
                cls._total_prompt_tokens / 1_000_000 * PRICE_INPUT_PER_M
                + cls._total_completion_tokens / 1_000_000 * PRICE_OUTPUT_PER_M
            )
            return {
                "calls": cls._total_calls,
                "promptTokens": cls._total_prompt_tokens,
                "completionTokens": cls._total_completion_tokens,
                "estimatedCostUsd": round(cost, 6),
            }

    @classmethod
    def reset_usage(cls) -> None:
        with cls._usage_lock:
            cls._total_prompt_tokens = 0
            cls._total_completion_tokens = 0
            cls._total_calls = 0

    @classmethod
    def _record_usage(cls, usage: dict[str, Any]) -> None:
        with cls._usage_lock:
            cls._total_prompt_tokens += int(usage.get("prompt_tokens") or 0)
            cls._total_completion_tokens += int(usage.get("completion_tokens") or 0)
            cls._total_calls += 1

    # ------------------------------------------------------------------ #
    # 底层请求                                                           #
    # ------------------------------------------------------------------ #

    def _post(
        self,
        image_bytes: bytes,
        mime: str,
        prompt: str,
        *,
        json_mode: bool,
        detail: str,
        max_tokens: int,
        client: httpx.Client | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """发一次多模态请求，带指数退避重试。返回 (文本内容, usage)。"""
        api_key = get_secret("deepseek_api_key")
        if not api_key:
            raise OcrUnavailable("尚未配置 DeepSeek API Key")

        payload: dict[str, Any] = {
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}",
                                "detail": detail,
                            },
                        },
                    ],
                }
            ],
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        http = client or _get_client()
        last_error: Exception | None = None
        # 本次尝试的输出预算。被截断时会翻倍重试（见下方 finish_reason 的处理）。
        attempt_max_tokens = max_tokens

        for attempt in range(MAX_RETRIES + 1):
            payload["max_tokens"] = attempt_max_tokens
            try:
                response = http.post(
                    "/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                )

                if response.status_code == 200:
                    data = response.json()
                    choices = data.get("choices") or []
                    if not choices:
                        raise OcrError("模型返回了空结果")
                    choice = choices[0]
                    content = (choice.get("message") or {}).get("content") or ""
                    finish_reason = choice.get("finish_reason")
                    usage = data.get("usage") or {}
                    self._record_usage(usage)

                    if finish_reason == "length":
                        # 输出预算被耗尽 —— **必须显式处理，不能当成"这张图没有内容"**。
                        #
                        # 推理模型先"思考"再作答，而思考 token 也算在 max_tokens 里，
                        # 所以预算不足时最常见的表现是：思考把额度吃光，content 变成
                        # 空字符串。若把它当成正常返回，用户看到的就是
                        # "任务成功、一个字都没识别出来"（实测 8192 预算下 100% 复现）。
                        reasoning = (usage.get("completion_tokens_details") or {}).get(
                            "reasoning_tokens"
                        )
                        detail = f"，其中思考占用 {reasoning}" if reasoning else ""
                        last_error = OcrError(
                            f"模型输出被截断（预算 {attempt_max_tokens} tokens{detail}）"
                        )
                        if attempt < MAX_RETRIES:
                            # 翻倍预算重试：预算是天花板不是花费，提高它不额外扣钱
                            attempt_max_tokens = min(attempt_max_tokens * 2, ABSOLUTE_MAX_TOKENS)
                            time.sleep(RETRY_BASE_DELAY)
                            continue
                        break

                    return content, usage

                # 认证/配额类错误重试没有意义，直接给出可读原因
                if response.status_code in (401, 403):
                    raise OcrUnavailable(
                        "DeepSeek API 鉴权失败，请检查 API Key 是否正确、是否已充值"
                    )
                if response.status_code == 402:
                    raise OcrUnavailable("DeepSeek 账户余额不足")
                if response.status_code == 400:
                    detail_text = ""
                    try:
                        detail_text = (response.json() or {}).get("error", {}).get("message", "")
                    except (ValueError, AttributeError):
                        detail_text = response.text[:200]
                    raise OcrError(f"请求被拒绝（400）：{detail_text}")

                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = OcrError(f"服务暂时不可用（HTTP {response.status_code}）")
                else:
                    raise OcrError(f"请求失败（HTTP {response.status_code}）：{response.text[:200]}")

            except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as err:
                last_error = OcrError(f"网络错误：{err}")
            except (OcrError, OcrUnavailable):
                raise

            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BASE_DELAY * (2**attempt))

        raise last_error or OcrError("请求失败")

    # ------------------------------------------------------------------ #
    # 结果解析                                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _text_to_blocks(text: str, tile_box: tuple[int, int, int, int]) -> list[TextBlock]:
        """把纯文本按行铺进切片区域。

        模型不返回坐标，所以按行在切片高度上**均匀分布**。
        这不追求像素级精确 —— 它的作用是让合并后的阅读顺序正确，
        以及在界面上能给出大致的位置示意。
        """
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            return []

        x0, y0, x1, y1 = tile_box
        line_height = max(1, (y1 - y0) // len(lines))
        blocks: list[TextBlock] = []
        for index, line in enumerate(lines):
            top = y0 + index * line_height
            blocks.append(
                TextBlock(text=line, box=(x0, top, x1, top + line_height), confidence=1.0)
            )
        return blocks

    @staticmethod
    def _parse_json_payload(text: str) -> dict[str, Any]:
        """从模型输出里取出 JSON。

        即便开了 JSON 模式，模型偶尔仍会包一层 ```json 代码块，这里做容错。
        """
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

        try:
            data = json.loads(cleaned)
            return data if isinstance(data, dict) else {"data": data}
        except json.JSONDecodeError:
            # 退而求其次：截取第一个 { 到最后一个 } 之间的内容
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start >= 0 and end > start:
                try:
                    data = json.loads(cleaned[start : end + 1])
                    return data if isinstance(data, dict) else {"data": data}
                except json.JSONDecodeError:
                    pass
        raise OcrError("模型返回的内容不是合法 JSON")

    # ------------------------------------------------------------------ #
    # 主流程                                                            #
    # ------------------------------------------------------------------ #

    def _detect_line_height_from(self, image: Image.Image) -> float:
        """在**已裁边的图片**上测量行高。

        裁边之后坐标系变了，必须重新测 —— 用原图的测量值会把切片规划带偏。
        先转成临时文件是因为本地引擎的接口收的是路径；裁边后的图通常小得多，
        这一次临时写盘的开销可以忽略。
        """
        import tempfile

        height = image.height
        handle = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        try:
            handle.close()
            image.save(handle.name)
            return self._detect_line_height(Path(handle.name))
        except Exception:  # noqa: BLE001 - 测量失败退回按图高估算
            return sanity_line_height(0.0, height)
        finally:
            try:
                Path(handle.name).unlink(missing_ok=True)
            except OSError:
                pass

    def _detect_line_height(self, path: Path) -> float:
        """尽量用本地 OCR 实测行高；不可用或明显不可信时退回按图高估算。

        用实测值而不是猜：切片是否必要、切多细，全都建立在这个数字上。
        但**实测值本身也可能不可信** —— 实测遇到过本地引擎在一张干净的
        表格图上返回 0 个文本框。这种情况必须当作"没测到"，
        而不是把 0 当成"字极小"的证据：后者会把一张小图切成两半。
        """
        with Image.open(path) as image:
            height = image.height

        try:
            from .local import get_engine as get_local

            local = get_local()
            if local.availability()[0]:
                blocks = local.detect_lines(path)
                measured = estimate_line_height(blocks)
                # 框太少（例如整图只有 1~2 个框）说明漏检严重，中位数没有代表性
                if measured > 0 and len(blocks) >= 3:
                    return sanity_line_height(measured, height)
        except Exception:  # noqa: BLE001 - 探测失败不该影响主流程
            pass

        return sanity_line_height(0.0, height)

    def recognize(
        self,
        image_path: Path | str,
        options: OcrOptions,
        progress: ProgressCallback | None = None,
    ) -> OcrResult:
        available, reason = self.availability()
        if not available:
            raise OcrUnavailable(reason or "云端 OCR 不可用")

        path = Path(image_path)
        if not path.is_file():
            raise OcrError(f"图片不存在：{path}")

        started = time.monotonic()

        # ---- 1. 缓存命中则直接返回，避免重复计费 ----
        cache = get_sync_cache()
        key = _cache_key(path, options)
        cached = cache.get(key)
        if cached is not None:
            if progress:
                progress(100, "命中缓存")
            result = _result_from_cache(cached)
            result.warnings.append("结果来自缓存（未重复调用云端，未产生费用）")
            return result

        # ---- 2. 规划切片 ----
        if progress:
            progress(8, "分析版面")

        from ..imaging import content_bbox

        origin_x = 0
        origin_y = 0
        trimmed_note = ""

        with Image.open(path) as opened:
            source = ImageOps.exif_transpose(opened).convert("RGB")
            width, height = source.size

            # 裁掉四周空白：模型按**整图**决定缩放比例，空白边距会白吃掉正文分辨率。
            # 实测一张 2400×3200、表格只占中间 900×1080 的照片，裁边前
            # 22px 的字被缩到 9px（认不出来），裁边后不再触发缩放。
            box = content_bbox(source)
            if box is not None:
                origin_x, origin_y = box[0], box[1]
                source = source.crop(box)
                kept = (source.width * source.height) / (width * height)
                # 只裁掉一点点就不必打扰用户 —— 那是内部优化，不是他需要知道的事。
                # 每张图都挂一条"已裁掉空白"会把真正的警告淹掉。
                if kept <= 0.85:
                    trimmed_note = (
                        f"已裁掉四周空白（{width}×{height} → {source.width}×{source.height}），"
                        "让模型把分辨率用在正文上"
                    )

        line_height = self._detect_line_height_from(source) if options.tiling != "off" else 0.0
        # 表格这类"结构由列定义"的内容只做横向切分：竖着切会把列结构劈碎，
        # 每块都只剩部分列、表头还对不上，实测会导致整列丢失。
        layout = "rows" if options.mode == "table" else "grid"
        tiles = plan_tiles(source.width, source.height, line_height, mode=options.tiling, layout=layout)

        prompt = build_prompt(
            options.mode, language=options.language, override=options.prompt_override
        )
        json_mode = options.mode in JSON_MODES

        warnings: list[str] = []
        if len(tiles) > 1:
            warnings.append(
                f"检测到字号偏小（约 {line_height:.0f}px），已自动切成 {len(tiles)} 块分别识别以保证准确率"
            )

        # 送进模型前会缩放到约 1300 等效像素。这里把"检测框高度"折算成
        # "字形高度"再判断可辨识度 —— 框高包含行距，直接用会高估近一倍，
        # 于是该提醒的不提醒。见 tiling.GLYPH_FROM_BOX_RATIO 的说明。
        scaled_glyph = line_height * GLYPH_FROM_BOX_RATIO * model_scale(source.width, source.height) if line_height else 0
        if 0 < scaled_glyph < MIN_RELIABLE_GLYPH_PX:
            warnings.append(
                f"字号偏小（缩放后字形约 {scaled_glyph:.0f}px，低于可靠的 {MIN_RELIABLE_GLYPH_PX:.0f}px），"
                "识别结果可能不准确甚至出现编造内容 —— 建议把表格拍得更近、更清晰后重试"
            )

        # 把影响正确性的提醒排在最前面：摘要只展示第一条，
        # 而"裁掉空白"这类是成功的优化说明，不该挤掉"结果可能不准"的警告。
        if trimmed_note:
            warnings.append(trimmed_note)

        # ---- 3. 逐块识别 ----
        tile_height = height
        tile_width = width

        def run_tile(index: int, tile_box: tuple[int, int, int, int]) -> tuple[Any, str, dict[str, Any]]:
            x0, y0, x1, y1 = tile_box
            crop = source.crop((x0, y0, x1, y1))
            data, mime = _encode_image(crop)

            with _cloud_semaphore:
                text, usage = self._post(
                    data,
                    mime,
                    prompt,
                    json_mode=json_mode,
                    detail=options.detail,
                    max_tokens=options.max_tokens,
                )
            return (x0, y0, x1, y1), text, usage

        tile_boxes = [tile.box for tile in tiles]
        results: list[tuple[tuple[int, int, int, int], str, dict[str, Any]]] = []

        def report(done: int) -> None:
            if progress:
                # 预留 10% 给最后的整理阶段
                progress(10 + 80 * done / max(1, len(tile_boxes)), f"识别中 {done}/{len(tile_boxes)}")

        if len(tile_boxes) == 1:
            results.append(run_tile(0, tile_boxes[0]))
            report(1)
        else:
            # 切片之间并行发送，显著缩短长文档的总耗时
            with ThreadPoolExecutor(max_workers=min(TILE_CONCURRENCY, len(tile_boxes))) as pool:
                futures = [pool.submit(run_tile, i, box) for i, box in enumerate(tile_boxes)]
                for done, future in enumerate(futures, start=1):
                    results.append(future.result())
                    report(done)

        source.close()

        # ---- 4. 合并 ----
        if progress:
            progress(92, "合并结果")

        merged_blocks: list[TextBlock] = []
        raw_tables: list[dict[str, Any]] = []
        raw_fields: dict[str, Any] = {}
        raw_texts: list[str] = []
        total_usage: dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0}

        for box, text, usage in results:
            total_usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            total_usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)

            if json_mode:
                try:
                    data = self._parse_json_payload(text)
                except OcrError:
                    # 单块 JSON 解析失败不该让整张图失败：降级当纯文本收下
                    warnings.append("有切片返回的内容无法解析为 JSON，已按纯文本处理")
                    merged_blocks.extend(self._text_to_blocks(text, box))
                    raw_texts.append(text)
                    continue

                if options.mode == "table":
                    for table in data.get("tables") or []:
                        if isinstance(table, dict):
                            raw_tables.append(table)
                    plain = str(data.get("text") or "")
                    if plain:
                        raw_texts.append(plain)
                    merged_blocks.extend(self._text_to_blocks(plain or json.dumps(data, ensure_ascii=False), box))
                elif options.mode == "card":
                    fields = data.get("fields")
                    if isinstance(fields, dict):
                        raw_fields.update(fields)
                    plain = str(data.get("text") or "")
                    if plain:
                        raw_texts.append(plain)
                    merged_blocks.extend(self._text_to_blocks(plain or json.dumps(data, ensure_ascii=False), box))
                else:
                    merged_blocks.extend(self._text_to_blocks(json.dumps(data, ensure_ascii=False), box))
            else:
                raw_texts.append(text)
                merged_blocks.extend(self._text_to_blocks(text, box))

        if len(tiles) > 1:
            merged_blocks = merge_tile_blocks(
                [
                    (tile, [b for b in merged_blocks if b.box[1] >= box[1] - 1 and b.box[3] <= box[3] + 1])
                    for tile, (box, _text, _usage) in zip(tiles, results, strict=True)
                ]
            )

        # 一个字、一张表都没有，且**每一次调用都返回了空内容** ——
        # 这不是"图上没内容"，而是调用出了问题（最典型的是输出预算被思考吃光）。
        # 必须显式报错：静默返回空结果，用户看到的是绿色的"成功"，
        # 根本不会想到要去重拍或换个识别模式。
        if not merged_blocks and not raw_tables and not any(text.strip() for text in raw_texts):
            raise OcrError(
                "云端未返回任何内容。可能原因：输出预算被模型的思考过程耗尽，"
                "或图片过于模糊/空白。请重试；若反复出现，请把图片拍得更清晰。"
            )

        ordered = sort_reading_order(merged_blocks)

        # 裁边后所有坐标都是"裁边图坐标系"，这里统一加回偏移量，
        # 让调用方拿到的始终是**原图坐标** —— 否则界面上的预览框会整体偏移。
        if origin_x or origin_y:
            ordered = [
                TextBlock(
                    text=block.text,
                    box=(
                        block.box[0] + origin_x,
                        block.box[1] + origin_y,
                        block.box[2] + origin_x,
                        block.box[3] + origin_y,
                    ),
                    confidence=block.confidence,
                )
                for block in ordered
            ]

        # 表格被切成多块时，每块都会返回"自己的那张表"（表头相同、行不同，
        # 重叠带那几行还会重复）。这里按表头把它们拼回一张，
        # 否则用户拿到的是同名且互相重复的好几张工作表。
        if raw_tables and len(tiles) > 1:
            merged_tables = merge_table_fragments(raw_tables)
            if len(merged_tables) < len(raw_tables):
                warnings.append(
                    f"表格跨 {len(raw_tables)} 个切片，已按表头合并为 {len(merged_tables)} 张"
                )
            raw_tables = merged_tables

        result = OcrResult(
            blocks=ordered,
            engine=self.id,
            width=tile_width,
            height=tile_height,
            language=options.language,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            tiles=len(tiles),
            usage={
                **total_usage,
                "imageTokensPerCall": MAX_IMAGE_TOKENS,
                "estimatedCostUsd": round(
                    total_usage["prompt_tokens"] / 1_000_000 * PRICE_INPUT_PER_M
                    + total_usage["completion_tokens"] / 1_000_000 * PRICE_OUTPUT_PER_M,
                    6,
                ),
            },
            warnings=warnings,
            raw={
                "tables": raw_tables,
                "fields": raw_fields,
                "texts": raw_texts,
                "jsonMode": json_mode,
            },
        )

        cache.put(key, _result_to_cache(result))
        if progress:
            progress(100, "完成")
        return result

    # ------------------------------------------------------------------ #
    # 成本预估                                                           #
    # ------------------------------------------------------------------ #

    def estimate_cost(self, image_path: Path | str, options: OcrOptions) -> dict[str, Any]:
        """粗略预估一次识别的费用。

        按"每块切片至少 1 个图片 token 上限 + 提示词 token"估算，用于界面上
        批量处理前的费用提示。刻意偏保守（高估），免得用户被账单吓到。
        """
        try:
            with Image.open(image_path) as image:
                width, height = image.size
        except OSError:
            return {}

        tiles = plan_tiles(width, height, height * UNKNOWN_LINE_RATIO, mode=options.tiling)
        prompt_tokens = len(build_prompt(options.mode, language=options.language)) // 2
        per_call = MAX_IMAGE_TOKENS + prompt_tokens
        calls = len(tiles)
        input_tokens = per_call * calls
        # 输出量取决于"思考"而不是内容长度，而且差异极大：实测纯文字模式
        # 思考仅 240 tokens，表格模式要 7000~11000。按模式分别估，
        # 免得给用户一个低一个数量级的费用预期。
        output_tokens = (12_000 if options.mode in JSON_MODES else 2_500) * calls

        return {
            "tiles": calls,
            "estimatedInputTokens": input_tokens,
            "estimatedOutputTokens": output_tokens,
            "estimatedCostUsd": round(
                input_tokens / 1_000_000 * PRICE_INPUT_PER_M
                + output_tokens / 1_000_000 * PRICE_OUTPUT_PER_M,
                6,
            ),
        }


# --------------------------------------------------------------------------- #
# 缓存序列化                                                                     #
# --------------------------------------------------------------------------- #

def _result_to_cache(result: OcrResult) -> dict[str, Any]:
    return {
        "blocks": [b.to_dict() for b in result.blocks],
        "engine": result.engine,
        "width": result.width,
        "height": result.height,
        "tiles": result.tiles,
        "usage": result.usage,
        "raw": result.raw,
        "warnings": result.warnings,
    }


def _result_from_cache(payload: dict[str, Any]) -> OcrResult:
    blocks = [
        TextBlock(
            text=item.get("text", ""),
            box=tuple(item.get("box", (0, 0, 0, 0))),  # type: ignore[arg-type]
            confidence=float(item.get("confidence", 1.0)),
        )
        for item in payload.get("blocks") or []
    ]
    return OcrResult(
        blocks=blocks,
        engine=str(payload.get("engine", "cache")),
        width=int(payload.get("width") or 0),
        height=int(payload.get("height") or 0),
        tiles=int(payload.get("tiles") or 1),
        usage=payload.get("usage") or {},
        raw=payload.get("raw") or {},
        warnings=list(payload.get("warnings") or []),
    )


_engine_instance: DeepSeekVisionEngine | None = None


def get_engine() -> DeepSeekVisionEngine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = DeepSeekVisionEngine()
    return _engine_instance
