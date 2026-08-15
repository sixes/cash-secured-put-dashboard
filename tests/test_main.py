from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app import main as main_mod
from app.params import defaults


def _engine():
    return SimpleNamespace(
        status=lambda: SimpleNamespace(label="market closed", quota_spent=0, quota_available=450)
    )


def _fake_templates(monkeypatch):
    """Render the shell and each panel as identifiable text.

    The real templates need a full market context; the route's contract here is which
    panels reach the response and in what order, so stub the rendering and keep the
    split-and-splice path exercised.
    """

    def get_template(name):
        if name == "dashboard.html":
            return SimpleNamespace(render=lambda ctx: f"HEAD{main_mod.PANEL_SLOT}TAIL")
        return SimpleNamespace(render=lambda ctx: f"[{ctx['p']['ticker']}]")

    monkeypatch.setattr(main_mod.templates, "get_template", get_template)


async def _read(response) -> str:
    return "".join([chunk async for chunk in response.body_iterator])


def _request(query: bytes = b"") -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "query_string": query})


@pytest.mark.anyio
async def test_index_streams_every_default_ticker(monkeypatch):
    tickers = tuple(f"T{i}" for i in range(20))
    calls: list[str] = []

    async def panel(engine, raw, params):
        calls.append(raw)
        return {"ticker": raw, "symbol": f"{raw}.US"}

    monkeypatch.setattr(main_mod, "settings", SimpleNamespace(default_tickers=tickers))
    monkeypatch.setattr(main_mod, "get_engine", _engine)
    monkeypatch.setattr(
        main_mod, "_resolve_params", lambda request: main_mod._Resolved(defaults(), None, None, [])
    )
    monkeypatch.setattr(main_mod, "_panel", panel)
    _fake_templates(monkeypatch)

    body = await _read(await main_mod.index(_request()))

    assert calls == list(tickers)
    assert body == "HEAD" + "".join(f"[{t}]" for t in tickers) + "TAIL"


@pytest.mark.anyio
async def test_index_keeps_failed_tickers_as_panels(monkeypatch):
    async def panel(engine, raw, params):
        if raw == "BAD":
            raise main_mod.UnknownTicker(raw)
        return {"ticker": raw, "symbol": f"{raw}.US"}

    seen: list[dict] = []

    def get_template(name):
        if name == "dashboard.html":
            return SimpleNamespace(render=lambda ctx: f"HEAD{main_mod.PANEL_SLOT}TAIL")
        return SimpleNamespace(render=lambda ctx: seen.append(ctx["p"]) or "")

    monkeypatch.setattr(main_mod, "settings", SimpleNamespace(default_tickers=("GOOD", "BAD")))
    monkeypatch.setattr(main_mod, "get_engine", _engine)
    monkeypatch.setattr(
        main_mod, "_resolve_params", lambda request: main_mod._Resolved(defaults(), None, None, [])
    )
    monkeypatch.setattr(main_mod, "_panel", panel)
    monkeypatch.setattr(main_mod.templates, "get_template", get_template)

    await _read(await main_mod.index(_request()))

    assert [p["ticker"] for p in seen] == ["GOOD", "BAD"]
    assert seen[1]["error"] == "BAD: no quote — check the symbol"


@pytest.mark.anyio
async def test_index_flushes_the_shell_before_any_panel_is_built(monkeypatch):
    """The whole point of streaming: the browser gets markup while builds are pending."""
    release = asyncio.Event()

    async def panel(engine, raw, params):
        await release.wait()
        return {"ticker": raw, "symbol": f"{raw}.US"}

    monkeypatch.setattr(main_mod, "settings", SimpleNamespace(default_tickers=("SPY",)))
    monkeypatch.setattr(main_mod, "get_engine", _engine)
    monkeypatch.setattr(
        main_mod, "_resolve_params", lambda request: main_mod._Resolved(defaults(), None, None, [])
    )
    monkeypatch.setattr(main_mod, "_panel", panel)
    _fake_templates(monkeypatch)

    response = await main_mod.index(_request())
    chunks = response.body_iterator

    first = await asyncio.wait_for(chunks.__anext__(), timeout=1.0)
    assert first == "HEAD"

    release.set()
    assert await asyncio.wait_for(chunks.__anext__(), timeout=1.0) == "[SPY]"


@pytest.mark.anyio
async def test_index_anchors_match_the_jump_bar(monkeypatch):
    """The bar is rendered from the requested tickers, before a symbol is resolved."""
    captured = {}

    def get_template(name):
        if name == "dashboard.html":
            def render(ctx):
                captured["nav"] = ctx["nav"]
                return f"HEAD{main_mod.PANEL_SLOT}TAIL"

            return SimpleNamespace(render=render)
        return SimpleNamespace(render=lambda ctx: captured.setdefault("anchors", []).append(
            ctx["p"]["anchor"]
        ) or "")

    async def stub(engine, raw, params):
        return {"ticker": raw, "symbol": f"{raw}.US", "anchor": main_mod._panel_anchor(raw)}

    monkeypatch.setattr(main_mod, "settings", SimpleNamespace(default_tickers=("brk.b", "spy")))
    monkeypatch.setattr(main_mod, "get_engine", _engine)
    monkeypatch.setattr(
        main_mod, "_resolve_params", lambda request: main_mod._Resolved(defaults(), None, None, [])
    )
    monkeypatch.setattr(main_mod, "_panel", stub)
    monkeypatch.setattr(main_mod.templates, "get_template", get_template)

    await _read(await main_mod.index(_request()))

    assert [n["anchor"] for n in captured["nav"]] == captured["anchors"]
    assert [n["ticker"] for n in captured["nav"]] == ["BRK.B", "SPY"]
