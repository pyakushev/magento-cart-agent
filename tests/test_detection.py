"""Platform / Cloudflare / page-kind heuristics on synthetic DOM snapshots."""
from __future__ import annotations

import magento_cart_agent as mca


def dom(**kw):
    base = {"url": "https://shop.de/", "title": "", "html": "", "visibleText": "", "hasMageCacheStorage": False, "magento": {}, "markers": {}, "scriptSrcs": [], "addToCartControls": 0, "cloudflare": {}}
    base.update(kw)
    return base


def test_cloudflare_challenge_title_blocks():
    markers = mca.detect_cloudflare(http_status=403, server_header="cloudflare", title="Nur einen Moment…", html="", visible_text="Sicherheitsüberprüfung wird durchgeführt", cookie_names=["__cf_bm"])
    assert markers
    assert any("403" in m for m in markers)
    assert "__cf_bm cookie" in markers


def test_cloudflare_bm_cookie_alone_is_not_a_block():
    assert mca.detect_cloudflare(http_status=200, server_header="cloudflare", title="Shop", html="<html>ok</html>", visible_text="Willkommen", cookie_names=["__cf_bm"]) == []


def test_cloudflare_challenge_script_with_opt_blocks():
    d = dom(cloudflare={"challengeScript": True, "opt": True, "widget": True, "inHtml": True})
    assert mca.detect_cloudflare(http_status=200, server_header="cloudflare", title="Shop", html="<script src='/cdn-cgi/challenge-platform/x'>", visible_text="", cookie_names=[], dom=d)


def test_403_with_cloudflare_server_blocks():
    assert mca.detect_cloudflare(http_status=403, server_header="cloudflare", title="Access denied", html="", visible_text="", cookie_names=[])


def test_a_normal_shop_on_cloudflare_is_not_blocked():
    d = dom(cloudflare={"challengeScript": False, "opt": False, "widget": True, "inHtml": False})
    assert mca.detect_cloudflare(http_status=200, server_header="cloudflare", title="Küchenrückwand", html="<html>shop</html>", visible_text="Produkte", cookie_names=["__cf_bm", "_ga"], dom=d) == []


def test_platform_magento_by_cache_storage():
    assert mca.detect_platform(dom(hasMageCacheStorage=True)) == "magento"


def test_platform_magento_by_markers_without_storage():
    d = dom(magento={"mageKeys": ["form_key"], "initScripts": 4, "staticVersion": True, "bodyClass": "cms-home"})
    assert mca.detect_platform(d, ["PHPSESSID"]) == "magento"


def test_platform_shopify_woocommerce_shopware_jtl():
    assert mca.detect_platform(dom(markers={"shopify": True})) == "shopify"
    assert mca.detect_platform(dom(markers={"woocommerce": True})) == "woocommerce"
    assert mca.detect_platform(dom(markers={"shopware": True})) == "shopware"
    assert mca.detect_platform(dom(markers={"jtl": True})) == "jtl"


def test_platform_shopify_by_cookie_when_scripts_are_hidden():
    assert mca.detect_platform(dom(), ["cart_currency", "_shopify_essential"]) == "shopify"


def test_page_kinds():
    assert mca.classify_page("https://s.de/", dom(magento={"bodyClass": "cms-home"})) == "homepage"
    assert mca.classify_page("https://s.de/checkout/cart/", dom()) == "cart"
    assert mca.classify_page("https://s.de/checkout/#shipping", dom()) == "checkout"
    assert mca.classify_page("https://s.de/onepagecheckout/", dom()) == "checkout"
    assert mca.classify_page("https://s.de/x.html", dom(addToCartControls=1)) == "product"
    assert mca.classify_page("https://s.de/kategorie/", dom()) == "category"


def test_cart_normalization():
    assert mca.normalize_cart(None)["in_cart"] is False
    assert mca.normalize_cart({"found": True, "cart": None})["in_cart"] is False
    out = mca.normalize_cart({"found": True, "cart": {"summary_count": 1, "items_count": 1, "items": ["Mock Bag"], "subtotal": "49"}})
    assert out["in_cart"] and out["summary_count"] == 1 and out["items"] == ["Mock Bag"]
    zero = mca.normalize_cart({"found": True, "cart": {"summary_count": 0, "items": [], "subtotal": 0}})
    assert zero["in_cart"] is False and zero["found"] is True


def test_agent_report_parsing():
    r = mca.parse_agent_report("Platform: Magento. verify_magento_cart returned in_cart: true. Reached delivery form at https://s.de/checkout/#shipping. Cloudflare: no.")
    assert r["platform_claim"] == "magento"
    assert r["in_cart_claim"] and r["reached_form_claim"] and not r["blocked_claim"]
    assert r["urls"] == ["https://s.de/checkout/#shipping"]




def test_blocked_at_launch_reports_unknown_platform(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import magento_cart_agent as mca

    class Page:
        url = "https://semilac.de/"

        async def goto(self, *a, **k):
            return SimpleNamespace(status=403, headers={"server": "cloudflare"})

        async def screenshot(self, path=None):
            return None

    class Ctx:
        def __init__(self):
            self.pages = [Page()]

        async def cookies(self):
            return [{"name": "__cf_bm", "domain": ".semilac.de"}]

        async def close(self):
            return None

    class Session:
        cdp_url = "http://127.0.0.1:1"
        page = Page()
        context = Ctx()

        async def close(self):
            return None

    async def fake_launch(opts):
        return Session()

    async def fake_dom(page, script, arg=None):
        return {"title": "Nur einen Moment…", "html": "<script src='/cdn-cgi/challenge-platform/x'></script>", "visibleText": "Sicherheitsüberprüfung wird durchgeführt", "magento": {"bodyClass": "page-layout-1column"}, "markers": {}, "cloudflare": {"inHtml": True, "challengeScript": True}}

    monkeypatch.setattr(mca, "launch_browser", fake_launch)
    monkeypatch.setattr(mca, "evaluate_safely", fake_dom)
    args = SimpleNamespace(screenshot_dir="checkout_screenshots", cdp_port=1, headless=True, profile_dir=None, channel=None,
                           executable_path=None, proxy=None, user_agent=None, cookies=None, stealth_js=None, max_steps=1, timeout=1)
    res, _ = asyncio.run(mca.process_url("https://semilac.de", 1, args, llm=None))
    assert res.status == "cloudflare_blocked"
    assert res.platform == "unknown"
    assert res.cloudflare is True


def test_status_decision_table():
    ok_ground = {"in_cart": True}
    no_ground = {"in_cart": False}
    no_cf = {"blocked": False}
    cf = {"blocked": True}
    d = mca.decide_status
    assert d(cf=no_cf, platform="magento", ground=ok_ground, report={}, reached_form=True) == "in_cart"
    assert d(cf=no_cf, platform="magento", ground=ok_ground, report={}, reached_form=False) == "in_cart_no_checkout"
    assert d(cf=no_cf, platform="shopify", ground=no_ground, report={}, reached_form=False) == "platform_mismatch"
    # unknown platform + no cart key: a gap in detection, not "the add failed on Magento"
    assert d(cf=no_cf, platform="unknown", ground=no_ground, report={}, reached_form=False) == "failed"
    assert d(cf=cf, platform="magento", ground=ok_ground, report={}, reached_form=True) == "cloudflare_blocked"
    assert d(cf=no_cf, platform="magento", ground=no_ground, report={"blocked_claim": True}, reached_form=False) == "blocked_by_agent"
