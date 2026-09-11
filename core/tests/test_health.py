"""内核冒烟测试。

重点验证三件事（这三件是前端能正常工作的前提）：
  1. 握手 Token 鉴权真的生效 —— 无 Token 必须被拒
  2. /api/health 与 /api/system/capabilities 可用且返回结构正确
  3. 引擎探测不会抛异常，且每个能力域至多标记一个"首选"
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from docforge.api.security import get_token
from docforge.app import create_app

TOKEN = get_token()
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def test_health_requires_token(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 401


def test_health_rejects_wrong_token(client: TestClient) -> None:
    resp = client.get("/api/health", headers={"Authorization": "Bearer not-the-real-token"})
    assert resp.status_code == 403


def test_health_ok(client: TestClient) -> None:
    resp = client.get("/api/health", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["phase"] == "ready"
    # 字段必须是 camelCase，才能与 TypeScript 侧契约对齐
    assert "uptimeSec" in body
    assert "jobsHandled" in body
    assert body["python"].startswith("Python")


def test_capabilities_shape(client: TestClient) -> None:
    resp = client.get("/api/system/capabilities", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()

    assert set(body) >= {
        "os",
        "cpu",
        "memory",
        "disk",
        "engines",
        "localOcrReady",
        "cloudOcrConfigured",
        "probedAt",
    }
    assert body["cpu"]["cores"] >= 1
    assert body["memory"]["totalBytes"] > 0
    assert isinstance(body["engines"], list) and body["engines"]

    for engine in body["engines"]:
        assert set(engine) >= {"id", "label", "domain", "available", "preferred", "fidelity"}


def test_at_most_one_preferred_per_domain(client: TestClient) -> None:
    engines = client.get("/api/system/capabilities", headers=AUTH).json()["engines"]

    preferred: dict[str, int] = {}
    for engine in engines:
        if engine["preferred"]:
            preferred[engine["domain"]] = preferred.get(engine["domain"], 0) + 1

    assert all(count <= 1 for count in preferred.values()), f"每个域只能有一个首选引擎：{preferred}"

    # 只有可用的引擎才可能被标记为首选
    for engine in engines:
        if engine["preferred"]:
            assert engine["available"] is True


def test_builtin_render_is_always_available(client: TestClient) -> None:
    """内置渲染引擎是最后一道防线，任何机器上都必须可用。"""
    engines = client.get("/api/system/capabilities", headers=AUTH).json()["engines"]
    builtin = next(e for e in engines if e["id"] == "office.builtin.render")
    assert builtin["available"] is True
