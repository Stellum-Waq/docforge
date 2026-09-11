"""设置与 OCR 引擎管理接口。

安全约定：**接口永不返回 API Key 明文**，只返回掩码。前端提交时若传空字符串，
表示"不修改"而非"清空"，避免用户误点保存把密钥弄丢。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..ocr import engine_status
from ..ocr.deepseek import get_engine as get_cloud_engine
from ..security.secrets import encryption_available, get_secret, load_secrets, mask_secret, save_secrets
from ..storage.cache import get_sync_cache
from .security import require_token

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


class SettingsBody(BaseModel):
    # 空字符串表示保持不变，避免误清空
    deepseekApiKey: str = Field("", description="留空表示不修改")
    cloudOcrEnabled: bool | None = None
    sensitivePatterns: list[str] | None = None
    clearApiKey: bool = False


@router.get("/settings")
async def read_settings() -> dict[str, Any]:
    """返回可安全暴露给界面的设置。密钥一律掩码。"""
    values = load_secrets()
    key = get_secret("deepseek_api_key")

    return {
        "deepseekApiKeyMasked": mask_secret(key),
        "hasApiKey": bool(key),
        "cloudOcrEnabled": bool(values.get("cloud_ocr_enabled", False)),
        "sensitivePatterns": values.get("sensitive_patterns") or [],
        "encryptionAvailable": encryption_available(),
        "cache": get_sync_cache().stats(),
    }


@router.put("/settings")
async def update_settings(body: SettingsBody) -> dict[str, Any]:
    payload: dict[str, Any] = {}

    if body.clearApiKey:
        payload["deepseek_api_key"] = ""
    elif body.deepseekApiKey.strip():
        payload["deepseek_api_key"] = body.deepseekApiKey.strip()

    if body.cloudOcrEnabled is not None:
        payload["cloud_ocr_enabled"] = body.cloudOcrEnabled

    if body.sensitivePatterns is not None:
        # 清洗：去掉空项与多余空白，避免用户粘进来一大段带换行的文本
        payload["sensitive_patterns"] = [
            item.strip() for item in body.sensitivePatterns if item and item.strip()
        ]

    if payload:
        save_secrets(payload)

    return await read_settings()


# --------------------------------------------------------------------------- #
# OCR 引擎                                                                #
# --------------------------------------------------------------------------- #

@router.get("/ocr/engines")
async def get_ocr_engines() -> dict[str, Any]:
    """各 OCR 引擎的可用性与不可用原因。"""
    return {
        "engines": engine_status(),
        "cloudEnabled": bool(load_secrets().get("cloud_ocr_enabled", False)),
        "hasApiKey": bool(get_secret("deepseek_api_key")),
    }


@router.get("/ocr/usage")
async def get_ocr_usage() -> dict[str, Any]:
    """云端识别的累计用量与估算费用。"""
    return {"usage": get_cloud_engine().usage_snapshot(), "cache": get_sync_cache().stats()}


@router.post("/ocr/usage/reset")
async def reset_ocr_usage() -> dict[str, Any]:
    get_cloud_engine().reset_usage()
    return {"ok": True}


@router.delete("/ocr/cache")
async def clear_ocr_cache() -> dict[str, Any]:
    """清空结果缓存。用于提示词调整后强制重新识别，或释放磁盘空间。"""
    get_sync_cache().clear()
    return {"ok": True, "cache": get_sync_cache().stats()}


@router.post("/ocr/test")
async def test_ocr_connection() -> dict[str, Any]:
    """用一次极小的请求验证 API Key 是否可用。

    刻意不发送真实图片：只发一句纯文本，成本可忽略，但足以验证
    鉴权与网络连通性 —— 用户填完密钥后能立刻知道对不对，不用等到
    处理一批文件才发现密钥是错的。
    """
    import httpx

    key = get_secret("deepseek_api_key")
    if not key:
        raise HTTPException(status_code=400, detail="尚未配置 API Key")

    try:
        response = httpx.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "deepseek-flash",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 4,
            },
            timeout=30.0,
        )
    except httpx.HTTPError as err:
        raise HTTPException(status_code=502, detail=f"网络请求失败：{err}") from err

    if response.status_code == 200:
        return {"ok": True, "message": "连接成功，API Key 可用"}

    if response.status_code in (401, 403):
        raise HTTPException(status_code=400, detail="API Key 无效或无权限")
    if response.status_code == 402:
        raise HTTPException(status_code=400, detail="账户余额不足，请先在 DeepSeek 平台充值")

    detail = ""
    try:
        detail = (response.json() or {}).get("error", {}).get("message", "")
    except (ValueError, AttributeError):
        detail = response.text[:200]
    raise HTTPException(status_code=502, detail=f"服务返回 HTTP {response.status_code}：{detail}")
