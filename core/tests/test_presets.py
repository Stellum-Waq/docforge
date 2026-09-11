"""参数预设的接口与持久化测试。

预设本质上是"一小段 JSON + 一个名字"，所以测试重点不在存储本身，而在
**语义约定**：同名覆盖、按动作隔离、删掉之后真的没了，
以及"预设里的参数最终有没有真的作用到任务上"。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from docforge.api.security import get_token
from docforge.app import create_app
from docforge.storage.db import get_db

AUTH = {"Authorization": f"Bearer {get_token()}"}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture(autouse=True)
def _clean_presets() -> None:
    """每个用例开始前清空预设，避免用例之间互相污染。"""
    import asyncio

    async def wipe() -> None:
        db = get_db()
        await db.connect()
        for preset in await db.list_presets():
            await db.delete_preset(preset["id"])

    asyncio.run(wipe())


def _save(client: TestClient, action: str, name: str, params: dict) -> dict:
    resp = client.post("/api/presets", json={"action": action, "name": name, "params": params}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    return resp.json()["preset"]


# --------------------------------------------------------------------------- #
# 基本读写                                                                      #
# --------------------------------------------------------------------------- #

def test_presets_require_token(client: TestClient) -> None:
    assert client.get("/api/presets").status_code == 401


def test_save_and_list_roundtrip(client: TestClient) -> None:
    saved = _save(client, "image.watermark", "公章", {"text": "公司公章", "opacity": 0.3})
    assert saved["name"] == "公章"
    assert saved["action"] == "image.watermark"
    assert saved["params"] == {"text": "公司公章", "opacity": 0.3}

    listing = client.get("/api/presets", headers=AUTH).json()["presets"]
    assert [p["name"] for p in listing] == ["公章"]
    # 中文与浮点都必须原样往返，不能被 JSON 序列化弄坏
    assert listing[0]["params"]["opacity"] == 0.3


def test_list_filters_by_action(client: TestClient) -> None:
    _save(client, "image.watermark", "公章", {"text": "A"})
    _save(client, "pdf.watermark", "机密件", {"text": "B"})

    only_pdf = client.get("/api/presets", params={"action": "pdf.watermark"}, headers=AUTH).json()
    assert [p["name"] for p in only_pdf["presets"]] == ["机密件"]

    # 不存在的动作应当明确 404，而不是默默返回空列表（那会让人以为"还没存"）
    resp = client.get("/api/presets", params={"action": "no.such.action"}, headers=AUTH)
    assert resp.status_code == 404


def test_saving_the_same_name_overwrites_instead_of_duplicating(client: TestClient) -> None:
    first = _save(client, "image.watermark", "公章", {"text": "旧", "opacity": 0.2})
    second = _save(client, "image.watermark", "公章", {"text": "新", "opacity": 0.6})

    assert first["id"] == second["id"], "同名预设应该复用 id，否则界面里会冒出两条同名记录"

    listing = client.get("/api/presets", headers=AUTH).json()["presets"]
    assert len(listing) == 1
    assert listing[0]["params"]["text"] == "新"


def test_same_name_on_different_actions_stays_separate(client: TestClient) -> None:
    _save(client, "image.watermark", "机密", {"text": "图片用"})
    _save(client, "pdf.watermark", "机密", {"text": "PDF 用"})

    listing = client.get("/api/presets", headers=AUTH).json()["presets"]
    assert len(listing) == 2
    assert {p["action"] for p in listing} == {"image.watermark", "pdf.watermark"}


def test_delete(client: TestClient) -> None:
    saved = _save(client, "image.watermark", "临时", {"text": "x"})

    assert client.delete(f"/api/presets/{saved['id']}", headers=AUTH).status_code == 200
    assert client.get("/api/presets", headers=AUTH).json()["presets"] == []
    # 再删一次应当 404，而不是"假装成功"
    assert client.delete(f"/api/presets/{saved['id']}", headers=AUTH).status_code == 404


# --------------------------------------------------------------------------- #
# 入参校验                                                                      #
# --------------------------------------------------------------------------- #

def test_unknown_action_is_rejected(client: TestClient) -> None:
    resp = client.post(
        "/api/presets",
        json={"action": "no.such.action", "name": "x", "params": {}},
        headers=AUTH,
    )
    assert resp.status_code == 404


@pytest.mark.parametrize("name", ["", "   "])
def test_blank_name_is_rejected(client: TestClient, name: str) -> None:
    resp = client.post(
        "/api/presets",
        json={"action": "image.watermark", "name": name, "params": {"text": "x"}},
        headers=AUTH,
    )
    assert resp.status_code == 400


def test_overlong_name_is_rejected(client: TestClient) -> None:
    resp = client.post(
        "/api/presets",
        json={"action": "image.watermark", "name": "长" * 41, "params": {}},
        headers=AUTH,
    )
    assert resp.status_code == 400


def test_name_is_trimmed(client: TestClient) -> None:
    saved = _save(client, "image.watermark", "  带空格  ", {"text": "x"})
    assert saved["name"] == "带空格"


def test_empty_params_are_allowed(client: TestClient) -> None:
    """"全部用默认值"也是一个合法的预设 —— 比强迫用户填一个占位参数好。"""
    saved = _save(client, "image.watermark", "默认", {})
    assert saved["params"] == {}


# --------------------------------------------------------------------------- #
# 预设真的作用到了任务上                                                          #
# --------------------------------------------------------------------------- #

def test_preset_params_reach_the_action(tmp_path) -> None:
    """预设里的参数必须真的影响产物，而不只是存在数据库里好看。"""
    from PIL import Image

    from docforge import cli

    image = tmp_path / "a.png"
    Image.new("RGB", (300, 200), (20, 24, 36)).save(image)

    assert cli.main([
        "docforge", "presets", "save", "image.watermark", "测试预设",
        "--param", "text=预设文字", "--param", "opacity=0.8",
    ]) == 0

    out_dir = tmp_path / "with-preset"
    assert cli.main([
        "docforge", "run", "image.watermark", str(image), "-o", str(out_dir), "--preset", "测试预设",
    ]) == 0

    produced = next(out_dir.glob("*.png"))
    assert produced.read_bytes() != image.read_bytes()

    # 换一套参数产物就不同 —— 说明上面那次确实用上了预设里的文案
    plain_dir = tmp_path / "plain"
    assert cli.main([
        "docforge", "run", "image.watermark", str(image), "-o", str(plain_dir),
        "--param", "text=别的文字", "--param", "opacity=0.8",
    ]) == 0
    plain = next(plain_dir.glob("*.png"))
    assert produced.read_bytes() != plain.read_bytes()


def test_build_params_merges_with_cli_taking_precedence() -> None:
    from docforge import cli

    cli.main([
        "docforge", "presets", "save", "image.watermark", "覆盖测试",
        "--param", "text=预设值", "--param", "opacity=0.3",
    ])

    merged = cli._build_params("image.watermark", "覆盖测试", ["text=命令行值"])
    assert merged["text"] == "命令行值"
    assert merged["opacity"] == 0.3, "未被覆盖的参数必须保留预设里的值"

    # 不套预设时行为不变
    assert cli._build_params("image.watermark", None, ["text=只有命令行"]) == {"text": "只有命令行"}


def test_build_params_raises_for_unknown_preset() -> None:
    from docforge import cli

    with pytest.raises(ValueError):
        cli._build_params("image.watermark", "并不存在的预设", [])


def test_load_preset_params_returns_a_copy() -> None:
    """预设是共享数据，取出来后修改不能污染数据库里的记录。"""
    from docforge import cli

    cli.main([
        "docforge", "presets", "save", "image.watermark", "副本测试", "--param", "text=原始",
    ])

    first = cli._load_preset_params("image.watermark", "副本测试")
    first["text"] = "被改过了"

    second = cli._load_preset_params("image.watermark", "副本测试")
    assert second["text"] == "原始"
