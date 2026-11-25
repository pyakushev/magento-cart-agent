"""URL handling, CLI defaults and .env loading — no browser needed."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import magento_cart_agent as mca


def test_normalize_url_prepends_https_only_without_scheme():
    assert mca.normalize_url("rupp-onlineshop.de") == "https://rupp-onlineshop.de"
    assert mca.normalize_url("http://shop.de") == "http://shop.de"
    assert mca.normalize_url("https://shop.de/a?b=1") == "https://shop.de/a?b=1"


def test_read_urls_skips_comments_blank_lines_and_duplicates(tmp_path):
    f = tmp_path / "urls.txt"
    f.write_text("# comment\n\n  shop.at  \nhttps://shop.at\nsecond.de\n", encoding="utf-8")
    assert mca.read_urls(str(f)) == ["https://shop.at", "https://second.de"]


def test_host_matching_allows_www_and_subdomains_only():
    assert mca.host_matches("https://www.shop.de/x", "shop.de")
    assert mca.host_matches("https://m.shop.de", "shop.de")
    assert mca.host_matches("https://shop.de", "www.shop.de")
    assert not mca.host_matches("https://other-shop.de", "shop.de")
    assert not mca.host_matches("https://shop.de.evil.com", "shop.de")


def test_deepseek_model_reference_maps_the_typo():
    assert mca.model_ref("deepseek", "deepseek-chat") == "deepseek:deepseek-chat"
    assert mca.model_ref("deepseek", "deepseek-flash") == "deepseek:deepseek-chat"
    assert mca.model_ref("openai", "gpt-5") == "openai:gpt-5"


def test_env_file_is_loaded_before_argparse_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("MAGENTO_MAX_STEPS", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    env = tmp_path / ".env"
    env.write_text("MAGENTO_MAX_STEPS=7\nMAGENTO_CDP_PORT=9876\n", encoding="utf-8")
    assert mca.load_env_before_defaults(["--env-file", str(env)]) == str(env)
    args = mca.build_parser().parse_args(["--env-file", str(env)])
    assert args.max_steps == 7
    assert args.cdp_port == 9876
    assert args.model == "deepseek-chat"


def test_flags_beat_environment(monkeypatch):
    monkeypatch.setenv("MAGENTO_MAX_STEPS", "9")
    args = mca.build_parser().parse_args(["--max-steps", "3"])
    assert args.max_steps == 3


def test_resolve_llm_requires_a_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    args = mca.build_parser().parse_args([])
    with pytest.raises(SystemExit) as exc:
        mca.resolve_llm(args)
    assert "DEEPSEEK_API_KEY" in str(exc.value)


def test_resolve_llm_uses_the_deepseek_reference(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    args = mca.build_parser().parse_args([])
    assert mca.resolve_llm(args) == "deepseek:deepseek-chat"
    assert args.resolved_provider == "deepseek"


def test_proxy_parsing_keeps_credentials_out_of_the_server():
    p = mca.parse_proxy("http://user:pa%40ss@proxy.local:8080")
    assert p["server"] == "http://proxy.local:8080"
    assert p["username"] == "user"
    assert "pa" in p["password"]


def test_launch_args_enable_cdp():
    args = mca.launch_args(9333)
    assert "--remote-debugging-port=9333" in args
    assert "--remote-allow-origins=*" in args


def test_gitignore_hides_secrets_and_artifacts():
    root = Path(__file__).resolve().parents[1]
    ignore = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    for needed in (".env", "checkout_screenshots/", ".playwright-browsers/"):
        assert needed in ignore, needed


def test_urls_argument_is_a_file_or_a_single_url(tmp_path):
    f = tmp_path / "u.txt"
    f.write_text("a.de\n", encoding="utf-8")
    assert mca.resolve_urls(str(f)) == ["https://a.de"]
    assert mca.resolve_urls("b.de") == ["https://b.de"]
    assert mca.resolve_urls("https://b.de/x") == ["https://b.de/x"]
    # a mistyped path must not silently become a URL
    assert mca.resolve_urls("/no/such/file.txt") == []
    assert mca.resolve_urls("./missing.txt") == []


class _FakeResult:
    """The shape of browser-use 1.0 AgentHistoryList (steps is an int, success may be None)."""

    def __init__(self, steps=7, success=None, judgement=None, error=None):
        self.steps, self.success, self.judgement, self.error = steps, success, judgement, error


class _Judgement:
    def __init__(self, success):
        self.success = success


def test_agent_steps_handles_an_int():
    """Regression: result.steps is an int in 1.0.6; len() crashed every run after the agent finished."""
    assert mca.agent_steps(_FakeResult(steps=15)) == 15
    assert mca.agent_steps(_FakeResult(steps=0)) is None
    assert mca.agent_steps(None) is None


def test_agent_steps_handles_a_list_from_older_versions():
    assert mca.agent_steps(_FakeResult(steps=[1, 2, 3])) == 3


def test_agent_success_falls_back_to_the_judge():
    assert mca.agent_success(_FakeResult(success=True)) is True
    assert mca.agent_success(_FakeResult(success=None, judgement=_Judgement(True))) is True
    assert mca.agent_success(_FakeResult(success=None, judgement=None)) is None
    assert mca.agent_success(None) is None


def test_report_understands_negations():
    r = mca.parse_agent_report("Platform: Magento. Cloudflare: no. I could not reach the delivery form. in_cart: false")
    assert r["platform_claim"] == "magento"
    assert r["cloudflare_claim"] is False
    assert r["reached_form_claim"] is False
    assert r["in_cart_claim"] is False
    r2 = mca.parse_agent_report("Magento shop, no Cloudflare. Cart verified: in_cart true. The delivery form is visible.")
    assert r2["in_cart_claim"] is True and r2["reached_form_claim"] is True and r2["cloudflare_claim"] is False


def test_goto_url_only_accepts_the_shop_host():
    import asyncio
    import json

    mca.SCOPE.cdp_url, mca.SCOPE.current_url = "http://127.0.0.1:1", "https://shop.de"
    tools = {t.name: t for t in mca.build_tools().functions}
    out = json.loads(asyncio.run(tools["goto_url"].function("https://evil.example/")))
    assert "error" in out and "only" in out["error"]
