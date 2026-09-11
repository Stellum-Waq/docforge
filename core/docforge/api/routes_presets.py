"""参数预设接口。

## 为什么值得单独做一层

办公文件处理有个很典型的特征：**同一套参数会被反复使用**。
"公司公章水印 + 30% 透明度 + 右下角"、"扫描件转可搜索 PDF + 200dpi + 中文"，
这些参数调一次就够了，但界面上每次都要重新填一遍 —— 重复劳动会让人
干脆放弃用工具，回去手动处理。

预设就是把这套参数存下来，下次一键套用。它同时也解决了**一致性**问题：
一个部门里所有人用同一个预设，输出的水印位置、字体大小才会完全一致。

## 设计取舍

* **按动作隔离**：预设绑定到 ``action_id``。跨动作套用参数是没有意义的
  （图片水印的 ``opacity`` 放到 PDF 水印里什么都不是），而且会静默产生
  错误配置。界面里也只会显示当前动作的预设。
* **同名覆盖**：``(action_id, name)`` 唯一，重新保存同名预设是更新而不是
  新建。否则用户很快会攒出一堆看不出区别的"水印1/水印2/水印3"。
* **不做参数校验**：预设存的就是一份参数字典。校验放在动作真正执行时，
  这样后端新增/删除参数不会让旧预设变成"坏数据"而无法加载 ——
  用户可以先套用旧预设，再手动补上新参数。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..actions import ActionError, get_action
from ..storage.db import get_db
from .security import require_token

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

#: 预设名长度上限 —— 只是为了不让界面上出现撑爆布局的名字
MAX_NAME_LEN = 40


class PresetBody(BaseModel):
    action: str = Field(..., description="动作 id，例如 image.watermark")
    name: str
    params: dict[str, Any] = Field(default_factory=dict)


@router.get("/presets")
async def list_presets(action: str | None = None) -> dict[str, Any]:
    """列出预设。带 ``action`` 参数时只返回该动作的预设。"""
    if action:
        try:
            get_action(action)
        except ActionError as err:
            raise HTTPException(status_code=404, detail=str(err)) from err

    presets = await get_db().list_presets(action)
    return {"presets": presets}


@router.post("/presets")
async def create_preset(body: PresetBody) -> dict[str, Any]:
    """新建或覆盖（同名）一个预设。"""
    try:
        get_action(body.action)
    except ActionError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err

    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="预设名不能为空")
    if len(name) > MAX_NAME_LEN:
        raise HTTPException(status_code=400, detail=f"预设名不能超过 {MAX_NAME_LEN} 个字符")

    preset = await get_db().save_preset(body.action, name, body.params)
    return {"preset": preset}


@router.delete("/presets/{preset_id}")
async def delete_preset(preset_id: str) -> dict[str, Any]:
    if not await get_db().delete_preset(preset_id):
        raise HTTPException(status_code=404, detail="预设不存在")
    return {"ok": True}
