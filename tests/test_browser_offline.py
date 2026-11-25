"""End-to-end pieces against a real headless Chromium and the local mock shops.

No LLM and no API key: the agent itself is exercised with pydantic-ai's TestModel,
the rest (CDP bridge, tools, ground-truth verification, screenshot) is real.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

import magento_cart_agent as mca


def test_launch_browser_exposes_cdp(browser, run):
    import urllib.request

    with urllib.request.urlopen(f"{browser.cdp_url}/json/version", timeout=5) as r:
        version = json.loads(r.read())
    assert "Browser" in version and "webSocketDebuggerUrl" in version


def test_bare_domain_does_not_navigate_but_normalized_url_does(browser, run, mock_shops):
    """A line without a scheme cannot be navigated; the normalized one is always https://host."""

    bare = mock_shops.split("//")[1]  # host:port, as a urls.txt line might look
    assert "://" not in bare
    assert mca.normalize_url(bare) == "https://" + bare  # scheme prepended, host kept

    async def check():
        await browser.page.goto(mca.normalize_url(bare) + "/", timeout=15_000)

    with pytest.raises(Exception) as info:  # the mock shop speaks http: https -> TLS error, not a navigation
        run(check())
    assert "ERR_" in str(info.value)


def test_verify_tool_reads_mage_cache_storage_and_ignores_other_hosts(browser, run, mock_shops, tools):
    """The tool reads the cart of the page it is pointed at — never a stale value parked elsewhere."""

    async def check():
        page = browser.page
        await page.goto(mock_shops + "/product/bag.html")
        await page.evaluate("localStorage.removeItem('mage-cache-storage')")
        await page.reload()
        await page.click("#product-addtocart-button")
        await asyncio.sleep(0.3)
        mca.SCOPE.cdp_url, mca.SCOPE.current_url = browser.cdp_url, mock_shops + "/product/bag.html"
        return await tools.call("verify_magento_cart")

    data = run(check())
    assert data["in_cart"] is True
    assert data["summary_count"] == 1
    assert data["items"] == ["Magento Mock Bag"]
    assert data["platform"] == "magento"


def test_cart_read_is_scoped_to_the_current_host(browser, run, mock_shops, tools):
    """A previous site's tab keeps its own mage-cache-storage: the read for the new host must not see it."""

    async def check():
        other_host = f"localhost:{mock_shops.rsplit(':', 1)[1]}"
        stale = await browser.new_page()
        await stale.goto(f"http://{other_host}/")
        await stale.evaluate("localStorage.setItem('mage-cache-storage', JSON.stringify({cart:{summary_count:9,items:[{product_name:'old site'}]}}))")
        await browser.page.goto(f"http://{other_host}/product/bag.html")
        await browser.page.evaluate("localStorage.setItem('mage-cache-storage', JSON.stringify({cart:{summary_count:1,items:[{product_name:'Magento Mock Bag'}]}}))")
        scoped = await mca.read_magento_cart(browser.cdp_url, f"http://{other_host}/product/bag.html")
        await stale.close()
        return scoped

    scoped = run(check())
    assert scoped["in_cart"] is True
    assert scoped["items"] == ["Magento Mock Bag"]
    assert scoped["summary_count"] == 1


def test_verify_tool_reports_platform_mismatch_on_shopify(browser, run, mock_shops, tools):
    async def call():
        await browser.page.goto(mock_shops + "/shopify/")
        mca.SCOPE.cdp_url, mca.SCOPE.current_url = browser.cdp_url, mock_shops + "/shopify/"
        return await tools.call("verify_magento_cart")

    data = run(call())
    assert data["in_cart"] is False
    assert data["platform"] == "shopify"
    assert "does not apply" in data["note"]


def test_inspect_tool_and_dismiss_overlays(browser, run, mock_shops, tools):
    async def check():
        page = browser.page
        await page.goto(mock_shops + "/overlay/")
        mca.SCOPE.cdp_url, mca.SCOPE.current_url = browser.cdp_url, mock_shops + "/overlay/"
        before = await tools.call("inspect_shop_page")
        dismissed = await tools.call("dismiss_overlays")
        after = await tools.call("inspect_shop_page")
        return before, dismissed, after

    before, dismissed, after = run(check())
    assert before["platform"] == "magento"
    assert before["consent_overlay_visible"] is True
    assert dismissed.get("clicked"), dismissed
    assert after["consent_overlay_visible"] is False


def test_read_magento_cart_ground_truth_after_real_click(browser, run, mock_shops, tools):
    async def check():
        page = browser.page
        await page.goto(mock_shops + "/catalog.html")
        await page.click("form[data-add='2'] button")
        await asyncio.sleep(0.4)
        return await mca.read_magento_cart(browser.cdp_url, mock_shops + "/catalog.html")

    cart = run(check())
    assert cart["in_cart"] is True and cart["summary_count"] == 1


def test_list_and_open_product_cards(browser, run, mock_shops, tools):
    async def check():
        page = browser.page
        await page.goto(mock_shops + "/catalog.html")
        mca.SCOPE.cdp_url, mca.SCOPE.current_url = browser.cdp_url, mock_shops + "/catalog.html"
        cards = await tools.call("list_product_cards")
        opened = await tools.call("open_product_card", index=0)
        return cards, opened, page.url

    cards, opened, final_url = run(check())
    assert cards["count"] >= 2
    assert any("Bag" in c["name"] for c in cards["cards"])
    assert opened.get("opened", "").endswith("/product/bag.html")


def test_capture_checkout_screenshot(browser, run, mock_shops, tmp_path, tools):
    async def check():
        page = browser.page
        await page.goto(mock_shops + "/product/bag.html")
        await page.click("#product-addtocart-button")
        await asyncio.sleep(0.3)
        await page.goto(mock_shops + "/checkout/")
        return await mca.capture_checkout(browser.cdp_url, mca.host_of(mock_shops), tmp_path / "checkout.png")

    path, url = run(check())
    assert path.endswith("checkout.png") and open(path, "rb").read(4) == b"\x89PNG"
    assert url.endswith("/checkout/")


def test_cloudflare_block_detected_end_to_end(browser, run, mock_shops, tools):
    async def check():
        page = browser.page
        response = await page.goto(mock_shops + "/blocked/")
        await asyncio.sleep(0.3)
        dom = await mca.evaluate_safely(page, mca.DOM_INFO_JS)
        cookies = await mca.cookies_of(browser.session.context)
        return mca.cloudflare_verdict(response.status, response.headers.get("server"), dom, cookies)

    verdict = run(check())
    assert verdict["blocked"] is True
    assert any("403" in m for m in verdict["markers"])
    assert "__cf_bm cookie" in verdict["markers"]


def test_overlay_swallows_clicks_until_dismissed(browser, run, mock_shops, tools):
    async def check():
        page = browser.page
        await page.goto(mock_shops + "/overlay/product/bag.html")
        with pytest.raises(Exception):
            await page.click("button.tocart", timeout=2_000)
        mca.SCOPE.cdp_url, mca.SCOPE.current_url = browser.cdp_url, mock_shops + "/overlay/product/bag.html"
        await tools.call("dismiss_overlays")
        await page.click("button.tocart", timeout=5_000)
        await asyncio.sleep(0.3)
        return await mca.read_magento_cart(browser.cdp_url, mock_shops + "/overlay/product/bag.html")

    cart = run(check())
    assert cart["in_cart"] is True


def test_checkout_watcher_freezes_on_the_delivery_form(browser, run, mock_shops, tools):
    async def check():
        page = browser.page
        await page.goto(mock_shops + "/checkout/")
        watcher = mca.CheckoutWatcher(browser.session.context)
        found = await watcher.scan()
        await watcher.stop()
        scroll = await page.evaluate("() => window.scrollY")
        return found, scroll

    found, scroll = run(check())
    assert found and found["hasForm"] is True
    assert found["hasStreet"] and found["hasCity"] and found["hasZip"]


def test_agent_runs_with_offline_test_model_over_cdp(browser, run, mock_shops, tools):
    from browser_use import Browser as BU_Browser
    from pydantic_ai.models.test import TestModel

    async def check():
        await browser.page.goto(mock_shops + "/product/bag.html")
        bu = BU_Browser(cdp_url=browser.cdp_url, keep_alive=True)
        mca.SCOPE.cdp_url, mca.SCOPE.current_url = browser.cdp_url, mock_shops + "/product/bag.html"
        try:
            result, watcher = await mca.run_agent(
                "add the product", TestModel(), bu, mca.build_tools(), browser.session.context, max_steps=3, timeout=60
            )
            return result, bu
        finally:
            await bu.stop()
            await watcher.stop()

    started = time.monotonic()
    result, bu = run(check())
    assert result is not None
    assert time.monotonic() - started < 120


@pytest.mark.parametrize("url_path,steps,expect", [
    ("/catalog.html", ["click form[data-add='2'] button"], True),
])
def test_scripted_shopper_adds_from_listing(browser, run, mock_shops, url_path, steps, expect, tools):
    """The same moves the agent is told to make, scripted: listing → add → cart page → checkout → screenshot."""

    async def check():
        page = browser.page
        await page.goto(mock_shops + url_path)
        for step in steps:
            await page.eval_on_selector_all(step.split(" ", 1)[1], "els => els[0].click()")
            await asyncio.sleep(0.4)
        cart = await mca.read_magento_cart(browser.cdp_url, mock_shops + url_path)
        await page.goto(mock_shops + "/checkout/cart/")
        await page.click("button.checkout")
        await page.wait_for_url("**/checkout/")
        return cart

    cart = run(check())
    assert cart["in_cart"] is expect


def test_goto_url_tool_navigates_the_shop_tab(browser, run, mock_shops, tools):
    """The tool that rescues the agent from about:blank: navigates the shop tab and reports the page."""

    async def check():
        await browser.page.goto(mock_shops + "/")
        mca.SCOPE.cdp_url, mca.SCOPE.current_url = browser.cdp_url, mock_shops + "/"
        return await tools.call("goto_url", url=mock_shops + "/product/bag.html")

    out = run(check())
    assert out["page_kind"] == "product"
    assert out["add_to_cart_controls"] >= 1
    assert browser.page.url.endswith("/product/bag.html")


def test_click_opening_a_new_tab_is_recoverable_with_goto_url(browser, run, mock_shops, tools):
    """A click that opens a new tab leaves the REPL tab behind: goto_url brings the product into it."""

    async def check():
        page = browser.page
        await page.goto(mock_shops + "/popup/")
        # the overlay covers the link: dismissing it is exactly what dismiss_overlays/the agent must do first
        await page.evaluate("() => document.querySelector('newsletter-popup').style.display = 'none'")
        await page.click("a.product-item-link")
        await asyncio.sleep(0.5)
        tabs = [p.url for p in browser.session.context.pages]
        mca.SCOPE.cdp_url, mca.SCOPE.current_url = browser.cdp_url, mock_shops + "/"
        out = await tools.call("goto_url", url=mock_shops + "/product/bag.html")
        return tabs, out

    tabs, out = run(check())
    assert len(tabs) >= 2  # window.open() opened a second tab
    assert out["page_kind"] == "product" and out["mage_cache_storage"] is True


def test_leftover_blank_tabs_are_closed_after_a_run(browser, run, mock_shops):
    async def check():
        ctx = browser.session.context
        blank = await ctx.new_page()
        await blank.goto("about:blank")
        before = len([p for p in ctx.pages if p.url == "about:blank"])
        await ctx.new_page()  # a content tab stays open
        await ctx.pages[-1].goto(mock_shops + "/catalog.html")
        await mca._calm_blanks(ctx)
        after = len([p for p in ctx.pages if p.url == "about:blank"])
        return before, after, len(ctx.pages)

    before, after, content_tabs = run(check())
    assert before >= 1
    assert after == 0
    assert content_tabs >= 1  # never closed the last real page
