#!/usr/bin/env python3
"""AI browser agent: add a Magento product to cart and capture the checkout form.

Given one URL per line, for every URL this script

  1. launches its own Playwright Chromium (persistent context, optional proxy /
     stealth / cookies) and exposes it over CDP,
  2. connects Browser-Use (1.0) to that exact browser via ``Browser(cdp_url=...)``,
  3. lets the agent decide on its own what page it landed on and where
     "Add to Cart" is,
  4. verifies the cart with a custom tool that reads Magento's
     ``localStorage['mage-cache-storage']`` over CDP,
  5. records Cloudflare-protected sites as blocked instead of fighting them,
  6. records non-Magento platforms (Shopify, WooCommerce, Shopware, JTL, …),
  7. goes through cart → checkout to the delivery/shipping address form, and
  8. screenshots that form (taken by this script, not the agent).

The agent's verdict is never trusted: after every run the cart is re-read
independently and only that (ground truth) decides ``in_cart``.

Quick start
-----------
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    playwright install chromium            # or run with --channel chrome|msedge
    cp .env.example .env                   # put DEEPSEEK_API_KEY there
    python magento_cart_agent.py --urls urls.txt

Useful flags (full list: ``--help``)
------------------------------------
    --urls FILE              one URL per line; '#' comments and blank lines ignored
    --llm auto|deepseek|openai|anthropic|google   (auto: DeepSeek when its key is set)
    --model deepseek-chat    DeepSeek model name; the agent gets 'deepseek:<model>',
                             which routes through pydantic-ai's DeepSeek provider
                             (thinking-mode tool_choice is handled there)
    --channel chrome|msedge  drive the installed browser instead of Playwright's
    --headless/--no-headless (default: headful — Cloudflare fingerprints it less)
    --proxy SCHEME://[user:pass@]host:port
    --cookies cookies.json   same format as Playwright's context.add_cookies()
    --stealth-js FILE        replacement JS init script (built-in stealth by default)
    --profile-dir DIR        persistent profile (default: throwaway temp dir)
    --stop-on-success        stop after the first verified cart
    --screenshot-dir DIR     (default: checkout_screenshots)
    --output results.json
    --max-steps 40 --timeout 900
    --detect-only            load every URL, record platform + Cloudflare, no agent,
                             no key, no cost
    --list-tools             build the agent against the offline test model and print
                             its tools — no browser, no key, no cost

Environment: every important flag has a MAGENTO_* variable (MAGENTO_MODEL,
MAGENTO_CDP_PORT, MAGENTO_HEADLESS, MAGENTO_MAX_STEPS, MAGENTO_TIMEOUT,
MAGENTO_SCREENSHOT_DIR, MAGENTO_OUTPUT, MAGENTO_PROXY, MAGENTO_USER_AGENT,
MAGENTO_COOKIES, MAGENTO_PROFILE_DIR, MAGENTO_CHANNEL); the key comes from
DEEPSEEK_API_KEY (.env is loaded before argparse reads the environment).

Statuses written to the results JSON
------------------------------------
    in_cart             ground-truth cart has items AND the delivery form was reached
    in_cart_no_checkout cart verified, but the delivery form was not reached
    platform_mismatch   non-Magento shop: a missing mage-cache-storage is expected,
                        not a failed add
    cloudflare_blocked  Cloudflare challenge / 403 / __cf_bm — recorded, not fought
    blocked_by_agent    the agent reported a CAPTCHA or a login wall inside the shop
    failed              the agent did not finish, or the run raised

Tests: ``python -m pytest tests -q`` (offline: a local mock of Magento, Shopify,
Cloudflare and an overlay-heavy shop; no API key needed).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

LOG = logging.getLogger("magento_cart_agent")


#: The spec named ``deepseek-flash``; the live DeepSeek API only serves
#: ``deepseek-chat`` and ``deepseek-reasoner``, so the alias maps to the former.
DEEPSEEK_MODEL_ALIASES = {"deepseek-flash": "deepseek-chat", "flash": "deepseek-chat"}
PROVIDER_KEYS = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
}
DEFAULT_OPENAI_MODEL = "gpt-5"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"
DEFAULT_GOOGLE_MODEL = "gemini-2.5-flash"
MAGENTO_ENV_DEFAULTS = {
    "model": "deepseek-chat",
    "cdp_port": "9222",
    "headless": "false",
    "max_steps": "40",
    "timeout": "900",
    "checkout_wait": "20",
    "screenshot_dir": "checkout_screenshots",
    "output": "results.json",
    "proxy": "",
    "user_agent": "",
    "cookies": "",
    "profile_dir": "",
    "channel": "",
}

STEALTH_JS = r"""
// Built-in stealth: the cheap, well-known fingerprint holes of automated Chromium.
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['de-DE', 'de', 'en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', {
  get: () => [{ name: 'PDF Viewer' }, { name: 'Chrome PDF Viewer' }, { name: 'Chromium PDF Viewer' }],
});
Object.defineProperty(navigator, 'mimeTypes', { get: () => [{ type: 'application/pdf' }] });
if (!window.chrome) window.chrome = { runtime: {} };
if (navigator.permissions && navigator.permissions.query) {
  const _query = navigator.permissions.query.bind(navigator.permissions);
  navigator.permissions.query = (p) =>
    p && p.name === 'notifications' ? Promise.resolve({ state: 'denied' }) : _query(p);
}
if (window.WebGLRenderingContext) {
  const _getParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (p) {
    if (p === 37445) return 'Intel Inc.';                // UNMASKED_VENDOR_WEBGL
    if (p === 37446) return 'Intel Iris OpenGL Engine';  // UNMASKED_RENDERER_WEBGL
    return _getParameter.call(this, p);
  };
}
"""


@dataclass
class LaunchOptions:
    """Everything needed to launch the browser this run owns."""

    cdp_port: int = 9222
    headless: bool = False
    profile_dir: str | None = None
    channel: str | None = None
    executable_path: str | None = None
    proxy: str | None = None
    user_agent: str | None = None
    viewport: tuple[int, int] = (1366, 820)
    cookies_file: str | None = None
    stealth_js: str | None = None
    locale: str = "de-DE"
    timezone_id: str = "Europe/Berlin"


@dataclass
class BrowserSession:
    """The browser one URL gets: our Playwright context plus its CDP endpoint."""

    cdp_url: str
    playwright: Any
    context: Any
    user_data_dir: str
    cleanup_dirs: list[str] = field(default_factory=list)
    page: Any = None

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.context.close()  # closes the Chromium we launched
        with contextlib.suppress(Exception):
            await self.playwright.stop()  # stops the Playwright driver
        for d in self.cleanup_dirs:
            shutil.rmtree(d, ignore_errors=True)


@dataclass
class UrlResult:
    """One line of the results JSON."""

    url: str
    platform: str | None = None
    cloudflare: bool = False
    status: str = "failed"
    agent_success: bool | None = None
    error: str | None = None
    cart: dict[str, Any] | None = None
    final_result: str | None = None
    screenshot: str | None = None
    cf_markers: list[str] = field(default_factory=list)
    page_kind: str | None = None
    checkout_url: str | None = None
    agent_reported: dict[str, Any] | None = None
    steps_used: int | None = None
    screenshots: dict[str, str] = field(default_factory=dict)
    duration_s: float | None = None


def normalize_url(url: str) -> str:
    """Bare domains do not navigate: ``shop.de`` becomes ``https://shop.de``."""
    return url if "://" in url else "https://" + url


def read_urls(path: str) -> list[str]:
    """One URL per line; blank lines and ``#`` comments are ignored, duplicates dropped."""
    urls: list[str] = []
    seen: set[str] = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        url = normalize_url(line)
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def host_of(url: str) -> str:
    try:
        return (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()
    except ValueError:
        return ""


def host_matches(page_url: str, target_host: str) -> bool:
    """Same host, a subdomain of it (``www.``, ``m.``), or its parent domain."""
    host = host_of(page_url)
    if not host or not target_host:
        return False
    return host == target_host or host.endswith("." + target_host) or target_host.endswith("." + host)


def resolve_urls(urls_arg: str) -> list[str]:
    """--urls is a file when it exists, else a single URL (a typo'd path must not become a URL)."""
    path = Path(urls_arg)
    if path.is_file():
        return read_urls(urls_arg)
    looks_like_url = urls_arg.startswith(("http://", "https://")) or (
        "." in urls_arg.split("/", 1)[0] and not urls_arg.startswith(("/", "./", "../", "~")) and " " not in urls_arg
    )
    return [normalize_url(urls_arg)] if looks_like_url else []


def _safe_name(url: str, index: int) -> str:
    return f"{index:02d}_{re.sub(r'[^a-zA-Z0-9.-]+', '_', host_of(url) or 'site')}"


async def _wait_cdp_ready(cdp_port: int, timeout: float = 30.0) -> str:
    """Poll ``http://127.0.0.1:<port>/json/version`` until Chromium answers."""

    def _get() -> dict[str, Any]:
        with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/version", timeout=1.5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            await asyncio.to_thread(_get)
            return f"http://127.0.0.1:{cdp_port}"
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last = exc
            await asyncio.sleep(0.25)
    raise RuntimeError(f"Chromium did not answer CDP on port {cdp_port} after {timeout:.0f}s ({last})")


def launch_args(cdp_port: int, locale: str = "de-DE") -> list[str]:
    """Chromium flags: the CDP port and the two switches modern Chromium needs."""
    return [
        f"--remote-debugging-port={cdp_port}",
        # newer Chromium rejects cross-origin CDP WebSocket connections without it
        "--remote-allow-origins=*",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        f"--lang={locale}",
    ]


def parse_proxy(proxy: str) -> dict[str, str]:
    """``scheme://[user:pass@]host[:port]`` → Playwright's proxy dict."""
    p = proxy if "://" in proxy else "http://" + proxy
    parts = urlsplit(p)
    out: dict[str, str] = {"server": f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")}
    if parts.username:
        out["username"] = parts.username
    if parts.password:
        out["password"] = parts.password
    return out


async def launch_browser(opts: LaunchOptions) -> BrowserSession:
    """Launch our own Chromium as a persistent context that speaks CDP.

    ``launch_persistent_context`` keeps exactly one context, so cookies and
    localStorage are the same for our CDP reads and Browser-Use's tabs. The
    ``user_data_dir`` is positional and required; without --profile-dir it is a
    throwaway ``tempfile.mkdtemp`` that ``close()`` removes.
    """
    from playwright.async_api import async_playwright

    cleanup_dirs: list[str] = []
    if opts.profile_dir:
        user_data_dir = str(Path(opts.profile_dir).expanduser().resolve())
        Path(user_data_dir).mkdir(parents=True, exist_ok=True)
    else:
        user_data_dir = tempfile.mkdtemp(prefix="magento-agent-profile-")
        cleanup_dirs.append(user_data_dir)

    kwargs: dict[str, Any] = {
        "headless": opts.headless,
        "args": launch_args(opts.cdp_port, opts.locale),
        "viewport": {"width": opts.viewport[0], "height": opts.viewport[1]},
        "locale": opts.locale,
        "timezone_id": opts.timezone_id,
    }
    if opts.proxy:
        kwargs["proxy"] = parse_proxy(opts.proxy)
    if opts.channel:
        kwargs["channel"] = opts.channel
    if opts.executable_path:
        kwargs["executable_path"] = opts.executable_path
    if opts.user_agent:
        kwargs["user_agent"] = opts.user_agent

    playwright = await async_playwright().start()
    try:
        context = await playwright.chromium.launch_persistent_context(user_data_dir, **kwargs)
    except Exception as exc:  # noqa: BLE001 - cleaned up and re-raised with a hint
        with contextlib.suppress(Exception):
            await playwright.stop()
        for d in cleanup_dirs:
            shutil.rmtree(d, ignore_errors=True)
        if "Executable doesn't exist" in str(exc):
            raise RuntimeError(
                "Playwright's Chromium is not installed. Run 'playwright install chromium', "
                "or drive the installed browser with --channel chrome (or msedge)."
            ) from exc
        raise

    session = BrowserSession(cdp_url=f"http://127.0.0.1:{opts.cdp_port}", playwright=playwright, context=context, user_data_dir=user_data_dir, cleanup_dirs=cleanup_dirs)
    try:
        stealth = ""
        if opts.stealth_js:
            stealth = Path(opts.stealth_js).read_text(encoding="utf-8")
        else:
            stealth = STEALTH_JS
        if stealth:
            await context.add_init_script(stealth)
        if opts.cookies_file:
            data = json.loads(Path(opts.cookies_file).read_text(encoding="utf-8"))
            cookies = data.get("cookies", []) if isinstance(data, dict) else data
            await context.add_cookies(cookies)
        session.cdp_url = await _wait_cdp_ready(opts.cdp_port)
        context.set_default_timeout(30_000)
        session.page = context.pages[0] if context.pages else await context.new_page()
        return session
    except Exception:
        await session.close()
        raise


async def connect_over_cdp(cdp_url: str) -> tuple[Any, Any]:
    """A second Playwright client attached to the same browser over CDP.

    Used by the tools and the screenshot step, which get no page handle in
    Browser-Use 1.0. ``browser.close()`` only disconnects — it never kills the
    shared browser; we own that separately.
    """
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(cdp_url)
    except Exception:
        with contextlib.suppress(Exception):
            await pw.stop()
        raise
    return pw, browser


def pages_of(browser: Any) -> list[Any]:
    pages: list[Any] = []
    for ctx in browser.contexts:
        pages.extend(p for p in ctx.pages if not p.is_closed())
    return pages


async def evaluate_safely(page: Any, script: str, arg: Any = None) -> Any:
    """Evaluate in a tab; a tab that navigated away must not kill the run."""
    try:
        return await (page.evaluate(script, arg) if arg is not None else page.evaluate(script))
    except Exception as exc:  # noqa: BLE001
        LOG.debug("evaluate failed on %s: %s", getattr(page, "url", "?"), exc)
        return None


async def cookies_of(browser_or_context: Any) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    contexts = getattr(browser_or_context, "contexts", None)
    for ctx in contexts if contexts is not None else [browser_or_context]:
        with contextlib.suppress(Exception):
            cookies += await ctx.cookies()
    return cookies


async def page_for_host(browser: Any, host: str) -> Any | None:
    """The tab for ``host`` (www./subdomains allowed), preferring real pages over about:blank."""
    matching = [p for p in pages_of(browser) if host_matches(getattr(p, "url", ""), host)]
    real = [p for p in matching if getattr(p, "url", "") not in ("", "about:blank")]
    return (real or matching or [None])[-1]


READ_MAGE_CART_JS = r"""
() => {
  let raw = null;
  try { raw = localStorage.getItem('mage-cache-storage'); } catch (e) { return { read_error: String(e) }; }
  if (raw == null) return { found: false };
  let data = null;
  try { data = JSON.parse(raw); } catch (e) { return { found: true, parse_error: String(e) }; }
  const cart = (data && data.cart) || null;
  if (!cart) return { found: true, cart: null };
  return { found: true, cart: {
    summary_count: cart.summary_count,
    items_count: cart.items ? cart.items.length : 0,
    items: (cart.items || []).map((i) => i.product_name || i.name || i.qty),
    subtotal: (cart.subtotalAmount && (cart.subtotalAmount.value || cart.subtotalAmount)) || cart.subtotal,
    url: cart['cart-url'] || null,
  } };
}
"""

DOM_INFO_JS = r"""
() => {
  const doc = document.documentElement;
  const html = doc ? doc.outerHTML : '';
  let keys = [];
  try { keys = Object.keys(localStorage); } catch (e) { keys = []; }
  const body = document.body || { className: '', innerText: '' };
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const url = location.href;
  const rawText = body.innerText || '';
  return {
    url: url,
    title: document.title || '',
    html: html,
    visibleText: rawText.replace(/\s+/g, ' ').slice(0, 1500),
    bodyLength: rawText.length,
    localStorageKeys: keys,
    hasMageCacheStorage: keys.includes('mage-cache-storage'),
    magento: {
      mageKeys: keys.filter((k) => /^mage-/.test(k) || /^(form_key|private_content_version|section_data_ids|product_data_storage)$/.test(k)),
      initScripts: document.querySelectorAll("script[type='text/x-magento-init']").length,
      xMagento: /x-magento|Magento_|mage\/|static\/version\d+\//.test(html),
      staticVersion: /\/static\/version\d+\//.test(html),
      require: !!window.require || !!window.requirejs,
      ko: !!window.ko,
      bodyClass: body.className || '',
      cartUrl: /\/checkout\/cart\/?/.test(html),
    },
    markers: {
      shopify: !!window.Shopify || /cdn\.shopify\.com|shopifyassets|Shopify\.theme/.test(html),
      woocommerce: !!window.woocommerce || /wc-block|woocommerce|wp-json\/wc/i.test(html),
      wordpress: /\/wp-content\//.test(html),
      shopware: !!window.shopware || /shopware/i.test(html.slice(0, 300000)),
      jtl: !!window.jtl || /jtl-shop|\/templates\/NOVA\//i.test(html),
      sylius: /sylius/i.test(html.slice(0, 200000)),
      prestashop: /prestashop/i.test(html.slice(0, 200000)),
      hybris: /hybris|sap-commerce/i.test(html.slice(0, 200000)),
    },
    scriptSrcs: [...document.scripts].map((s) => s.src).filter(Boolean).slice(0, 50),
    addToCartControls: [...document.querySelectorAll(
      "form[action*='cart/add'], #product-addtocart-button, .action.tocart, .single_add_to_cart_button, [data-testid*='add-to-cart'], .btn-cart"
    )].filter(vis).length,
    consentBlockers: (() => {
      // a consent layer must carry consent wording — a newsletter block that merely says "Cookies" is not an overlay
      const word = /akzeptier|accept all|accept cookies|zustimm|stimme zu|einverstanden|cookies? (verwenden|nutzen|einsetzen)/i;
      const hosts = [...document.querySelectorAll("[id*='consent' i],[class*='consent' i],[id*='cookie' i],[class*='cookie' i],[id*='usercentrics' i],[id*='cybot' i],[id*='didomi' i],[class*='borlabs' i],[id*='erecht24' i],cookie-consent-banner,cookie-consent")];
      const dialogs = [...document.querySelectorAll("[role='dialog'], [class*='modal' i], [class*='popup' i], [class*='overlay' i]")];
      return [...hosts.filter((e) => vis(e) && e.offsetHeight > 40), ...dialogs.filter((e) => vis(e) && e.offsetHeight > 80 && word.test(e.innerText || ''))].length;
    })(),
    captchaVisible: [...document.querySelectorAll("iframe[src*='recaptcha'], iframe[src*='hcaptcha'], .g-recaptcha, .h-captcha, [id*='cf-turnstile'], [class*='captcha']")].filter(vis).length > 0,
    cloudflare: {
      challengeScript: /cdn-cgi\/challenge-platform/.test(" " + [...document.scripts].map((s) => s.src || '').join(' ')),
      inHtml: /cdn-cgi\/challenge-platform|_cf_chl_opt|cf-challenge|challenge-form/i.test(html),
      widget: document.querySelectorAll("[id^='cf-chl-widget'], .cf-turnstile, iframe[src*='challenges.cloudflare.com']").length > 0,
      opt: typeof window.__cf_chl_opt !== 'undefined',
    },
  };
}
"""

CHECKOUT_SCAN_JS = r"""
() => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight);
  const fields = [...document.querySelectorAll('input, select')].filter(
    (e) => vis(e) && !['hidden', 'submit', 'button', 'checkbox', 'radio'].includes(e.type) && !/search/i.test((e.name || '') + (e.id || '')));
  const key = (e) => ((e.name || '') + (e.id || '') + (e.autocomplete || '')).toLowerCase();
  const hasStreet = fields.some((e) => /street|address-line|adresse|strasse|straße|line1|addr/.test(key(e)));
  const hasCity = fields.some((e) => /city|stadt|^ort|town/.test(key(e)));
  const hasZip = fields.some((e) => /zip|postcode|postal|plz/.test(key(e)));
  const hasEmail = fields.some((e) => e.type === 'email' || /e-?mail/.test(key(e)));
  const hasForm = hasStreet && hasCity && hasZip;
  if (hasForm) {
    const anchor = fields.find((e) => /street/.test(key(e))) || fields[0];
    const top = anchor.getBoundingClientRect().top + window.scrollY - 120;
    window.scrollTo({ top: Math.max(0, top), behavior: 'instant' });
  }
  return {
    url: location.href, title: document.title.slice(0, 140), hasForm,
    hasEmail, hasStreet, hasCity, hasZip,
    fields: fields.slice(0, 24).map((e) => e.name || e.id || e.type),
  };
}
"""

PRODUCT_CARDS_JS = r"""
() => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight);
  const seen = new Set();
  const out = [];
  const add = (name, el) => {
    name = (name || '').replace(/\s+/g, ' ').trim().slice(0, 140);
    if (!name || name.length < 2 || seen.has(el)) return;
    seen.add(el);
    out.push({ name });
  };
  const CARD = "form[action*='cart/add'], .product-item-info, li.item.product, .product-item, .product-card, .product-tile, .card-product";
  for (const box of document.querySelectorAll(CARD)) {
    if (!vis(box)) continue;
    const nameEl = box.querySelector('.product-item-link, .product-name, [itemprop=name], h2, h3, a[href]');
    if (nameEl) add(nameEl.innerText || nameEl.getAttribute('title'), box);
  }
  // generic grids: a product link with a product-looking path, regardless of theme markup
  const PRODUCT_LINK = /\/(product|products|p|produkt|artikel|item)\/|[^/]+\.html?$|\/products\//i;
  for (const a of document.querySelectorAll("a[href]")) {
    if (!vis(a) || !PRODUCT_LINK.test(a.getAttribute('href') || '')) continue;
    if (/cart|checkout|kasse|konto|account|login|agb|impressum|datenschutz|newsletter/i.test(a.getAttribute('href') || '')) continue;
    const name = a.innerText || a.getAttribute('title') || a.getAttribute('aria-label');
    if (!name || name.trim().length < 3) continue;
    add(name, a.closest(CARD) || a);
  }
  return out.slice(0, 40);
}
"""
OPEN_CARD_JS = r"""
(index) => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight);
  const boxes = [...document.querySelectorAll(
    "form[action*='cart/add'], .product-item-info, li.item.product, .product-item, [itemtype*='Product'], .product-card, .card-product, .product-tile"
  )].filter(vis);
  const box = boxes[index];
  if (!box) return { error: 'index out of range' };
  const link = box.querySelector("a.product-item-link[href], a[href$='.html'], a[href*='/products/']");
  if (link) { link.scrollIntoView({ block: 'center' }); link.click(); return { opened: link.href }; }
  const ctrl = box.querySelector("button, input[type=submit], .action.tocart");
  if (ctrl) { ctrl.scrollIntoView({ block: 'center' }); ctrl.click(); return { added_via_card: true }; }
  return { error: 'no link or add button in that card' };
}
"""

DISMISS_OVERLAYS_JS = r"""
() => {
  const labels = [
    /^alle akzeptieren$/i, /^alles akzeptieren$/i, /^alle cookies akzeptieren$/i, /^accept all( cookies)?$/i,
    /^akzeptieren$/i, /^ich stimme zu$/i, /^zustimmen$/i, /^einverstanden$/i, /^accept$/i, /^ok$/i,
    /^nur notwendige akzeptieren$/i, /^ablehnen$/i, /^weiter zur seite$/i, /^schließen$/i, /^close$/i,
  ];
  const visible = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const roots = [document];
  // known consent managers render into a shadow root: walk it (open roots only)
  const hosts = [...document.querySelectorAll(
    '#usercentrics-root, #CookiebotWidget, [id*=cookiefirst i], [id*=didomi i], #didomi-host, [class*=borlabs i],' +
    'cookie-consent-banner, cookie-consent, [class*=consent-banner i], [class*=cookie-banner i], [id*=consent-banner i]'
  )];
  for (const host of hosts) {
    if (host.shadowRoot) roots.push(host.shadowRoot);
  }
  // custom elements anywhere may host the banner in their own shadow root
  for (const el of document.querySelectorAll('*')) {
    if (el.tagName.includes('-') && el.shadowRoot && roots.indexOf(el.shadowRoot) < 0) roots.push(el.shadowRoot);
  }
  for (const re of labels) {
    for (const root of roots) {
      const hit = [...root.querySelectorAll("button, a, [role=button], input[type=button], input[type=submit], span, div")]
        .find((e) => re.test((e.innerText || e.value || '').trim()) && visible(e));
      if (hit) {
        const label = (hit.innerText || hit.value || '').trim().slice(0, 60);
        hit.click();
        return { clicked: label, in_shadow_dom: root !== document };
      }
    }
  }
  // a consent manager that only loaded its script: start it, the next call finds the button
  const w = window;
  if (w.Cookiebot && typeof w.Cookiebot.show === 'function') { try { w.Cookiebot.show(); } catch (e) {} return { clicked: null, started: 'Cookiebot', retry: true }; }
  if (w.UC_UI && typeof w.UC_UI.showSecondLayer === 'function') { return { clicked: null, note: 'Usercentrics UI present' }; }
  return { clicked: null };
}
"""

REMOVE_COVER_JS = r"""
() => {
  let removed = 0;
  const word = /akzeptier|accept all|zustimm|stimme zu|einverstanden|cookies? (verwenden|nutzen|einsetzen)/i;
  const visible = (e) => !!(e.offsetWidth || e.offsetHeight);
  for (const e of document.querySelectorAll("[id*='consent' i],[class*='consent' i],[id*='cookie' i],[class*='cookie' i],[id*='usercentrics' i],[id*='cybot' i],[role='dialog'],[class*='modal' i]")) {
    if (!visible(e)) continue;
    // a consent layer or a fixed full-viewport cover that asks for consent
    const s = getComputedStyle(e);
    const covers = s.position === 'fixed' && e.offsetHeight > window.innerHeight * 0.6;
    if (!covers && !word.test(e.innerText || '')) continue;
    e.style.setProperty('display', 'none', 'important');
    removed++;
  }
  document.documentElement.style.overflow = '';
  document.body.style.overflow = '';
  return { removed };
}
"""


def detect_cloudflare(
    *,
    http_status: int | None,
    server_header: str | None,
    title: str,
    html: str,
    visible_text: str,
    cookie_names: list[str],
    dom: dict[str, Any] | None = None,
) -> list[str]:
    """The Cloudflare markers present on the page (empty list = not blocked).

    Weak signals (an ``__cf_bm`` cookie, a Turnstile widget on a live shop) only
    count when a strong page-level signal backs them up — otherwise every
    Cloudflare-fronted shop would be "blocked".
    """
    dom = dom or {}
    cf = dom.get("cloudflare") or {}
    server_cf = "cloudflare" in (server_header or "").lower()
    t = (title or "").lower()
    text = (visible_text or "").lower()
    h = (html or "").lower()
    strong: list[str] = []
    if http_status == 403 and server_cf:
        strong.append("HTTP 403 with Server: cloudflare")
    for needle in ("just a moment", "nur einen moment", "attention required", "checking your browser", "un instant", "verifying you are human", "sicherheitsüberprüfung wird durchgeführt"):
        if needle in t:
            strong.append(f'title "{title[:70]}"')
            break
    for needle in ("sicherheitsüberprüfung wird durchgeführt", "verifying you are human", "checking your browser before", "bestätigen, dass sie ein mensch"):
        if needle in text:
            strong.append("challenge text")
            break
    if cf.get("inHtml") or cf.get("challengeScript"):
        strong.append("challenge-platform script")
    if cf.get("widget") and (cf.get("opt") or "cf-chl-widget" in h):
        strong.append("managed challenge widget")
    if "/cdn-cgi/challenge-platform" in h and cf.get("opt"):
        strong.append("__cf_chl_opt")
    weak: list[str] = []
    if "__cf_bm" in cookie_names:
        weak.append("__cf_bm cookie")
    if "cf_clearance" in cookie_names:
        weak.append("cf_clearance cookie")
    if cf.get("widget") and not strong:
        weak.append("turnstile widget")
    if not strong:
        return []
    return strong + [w for w in weak if w != "turnstile widget"]


def detect_platform(dom: dict[str, Any] | None, cookie_names: list[str] | None = None) -> str:
    """Best guess of the shop platform from what the page itself shows (Magento first)."""
    if not dom:
        return "unknown"
    m = dom.get("magento") or {}
    marks = dom.get("markers") or {}
    if dom.get("hasMageCacheStorage"):
        return "magento"
    score = (
        (3 if m.get("mageKeys") else 0)
        + (2 if m.get("initScripts") else 0)
        + (1 if m.get("staticVersion") else 0)
        + (1 if m.get("xMagento") else 0)
        + (1 if m.get("ko") else 0)
        + (1 if re.search(r"\b(cms-home|cms-index-index|checkout-cart-index|customer-account-login|page-layout-\w+)", m.get("bodyClass") or "") else 0)
    )
    if score >= 3:
        return "magento"
    for name in ("shopify", "woocommerce", "shopware", "jtl", "sylius", "prestashop", "hybris"):
        if marks.get(name):
            return name
    blob = " ".join(cookie_names or [])
    if "cart_currency" in blob or "_shopify_essential" in blob:
        return "shopify"
    if "wp_woocommerce_session" in blob:
        return "woocommerce"
    return "magento-ish" if score >= 1 else "unknown"


def classify_page(url: str, dom: dict[str, Any] | None = None) -> str:
    """What page we are on — for reporting only; the flow never branches on it."""
    path = urlsplit(url or "").path.lower()
    body = ((dom or {}).get("magento") or {}).get("bodyClass") or ""
    add = (dom or {}).get("addToCartControls") or 0
    if "/checkout/cart" in path or re.search(r"\bcheckout-cart-index\b", body):
        return "cart"
    if re.search(r"(onepagecheckout|onepage|/checkout)", (url or "").lower()) or re.search(r"\b(checkout|onepagecheckout)\b", body):
        return "checkout"
    if add:
        return "product"
    if path in ("", "/") or "cms-home" in body:
        return "homepage"
    if re.search(r"catalogsearch|/search|404|no-route|/page/", path):
        return "search_or_error"
    return "category"


def normalize_cart(data: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize a raw mage-cache-storage read to what the model and the loop can act on."""
    out = {"in_cart": False, "found": False, "summary_count": 0, "items_count": 0, "items": [], "subtotal": None}
    if not data:
        return out
    out["found"] = bool(data.get("found"))
    cart = data.get("cart") or {}
    items = list(cart.get("items") or [])
    summary = int(cart.get("summary_count") or 0)
    items_count = int(cart.get("items_count") or len(items))
    out.update(
        summary_count=summary,
        items_count=items_count,
        items=[str(i) for i in items[:10] if i],
        subtotal=cart.get("subtotal"),
        in_cart=bool(out["found"] and (summary > 0 or items_count > 0)),
    )
    return out


class AgentScope:
    """What the tools need to know: the CDP endpoint and the URL under test.

    Browser-Use 1.0 passes a tool only the arguments the model chose, so the
    tools read this module-level scope and connect over CDP themselves.
    """

    def __init__(self, cdp_url: str = "", current_url: str = ""):
        self.cdp_url = cdp_url
        self.current_url = current_url

    @property
    def host(self) -> str:
        return host_of(self.current_url)


SCOPE = AgentScope()


def set_current_url(url: str) -> None:
    SCOPE.current_url = url


async def read_magento_cart(cdp_url: str, url: str) -> dict[str, Any]:
    """Independent (ground-truth) cart read, scoped to the host of ``url``.

    Old tabs of previous URLs keep their own mage-cache-storage: without the
    scope a previous site's cart would leak into this result.
    """
    host = host_of(url)
    pw, browser = await connect_over_cdp(cdp_url)
    try:
        pages = pages_of(browser)
        scoped = [p for p in pages if host_matches(getattr(p, "url", ""), host)]
        first: dict[str, Any] | None = None
        for page in reversed(scoped):  # newest tab first
            data = await evaluate_safely(page, READ_MAGE_CART_JS)
            if data is None:
                continue
            if first is None:
                first = data
            if data.get("found") and data.get("cart"):
                return {**normalize_cart(data), "read_from": page.url}
        out = normalize_cart(first)
        out["read_from"] = scoped[-1].url if scoped else None
        if not scoped:
            out["note"] = f"no open tab for {host}"
        return out
    finally:
        with contextlib.suppress(Exception):
            await browser.close()  # disconnects only
        with contextlib.suppress(Exception):
            await pw.stop()


async def _dom_of(browser: Any, host: str) -> tuple[Any, dict[str, Any] | None]:
    pages = pages_of(browser)
    candidates = [p for p in pages if host_matches(getattr(p, "url", ""), host)] if host else []
    if not candidates:
        candidates = [p for p in pages if getattr(p, "url", "") not in ("", "about:blank")]
    for page in reversed(candidates):
        dom = await evaluate_safely(page, DOM_INFO_JS)
        if dom:
            return page, dom
    return (candidates[-1] if candidates else None), None


def build_tools() -> Any:
    """Register verify_magento_cart plus three helpers for inspection and overlays."""
    from browser_use import Tools

    tools = Tools()

    @tools.action(
        "Verify the Magento shopping cart. Reads the 'mage-cache-storage' key from the current shop page's "
        "localStorage over CDP and reports whether an item is actually in the cart. Call it (as a tool call, "
        "not as Python code) after clicking 'Add to Cart'. Returns JSON with 'in_cart', 'summary_count', "
        "'items_count', 'items', 'found', 'platform'."
    )
    async def verify_magento_cart() -> str:
        scope = SCOPE
        pw, browser = await connect_over_cdp(scope.cdp_url)
        try:
            page = await page_for_host(browser, scope.host) if scope.host else None
            if page is None:
                return json.dumps({"in_cart": False, "found": False, "error": f"no open tab for {scope.host or 'the current shop'}"}, ensure_ascii=False)
            data = await evaluate_safely(page, READ_MAGE_CART_JS) or {"found": False}
            dom = await evaluate_safely(page, DOM_INFO_JS) or {}
            names = [c.get("name", "") for c in await cookies_of(browser)]
            platform = detect_platform(dom, names) if dom else "unknown"
            out = normalize_cart(data)
            out.update(url=getattr(page, "url", None), platform=platform)
            if not out["found"] and platform not in ("magento", "unknown", "magento-ish"):
                out["note"] = (
                    f"no mage-cache-storage on this page: the shop looks like {platform}, so the Magento cart check "
                    "does not apply here (report the platform instead of retrying the Magento check)"
                )
            return json.dumps(out, ensure_ascii=False)
        finally:
            with contextlib.suppress(Exception):
                await browser.close()
            with contextlib.suppress(Exception):
                await pw.stop()

    @tools.action(
        "Inspect the current shop tab: returns the platform (magento/shopify/woocommerce/shopware/jtl/…), whether "
        "Cloudflare is challenging the page, the page kind done by heuristics (product/category/homepage/cart/"
        "checkout/…), the number of visible add-to-cart controls, whether a consent overlay is visible, a few "
        "localStorage keys and a short text excerpt. Look, don't assume."
    )
    async def inspect_shop_page() -> str:
        scope = SCOPE
        pw, browser = await connect_over_cdp(scope.cdp_url)
        try:
            page, dom = await _dom_of(browser, scope.host)
            if page is None or dom is None:
                return json.dumps({"error": "no readable tab"})
            names = [c.get("name", "") for c in await cookies_of(browser)]
            markers = detect_cloudflare(
                http_status=None, server_header=None, title=dom.get("title", ""), html=dom.get("html", ""),
                visible_text=dom.get("visibleText", ""), cookie_names=names, dom=dom,
            )
            out = {
                "url": dom.get("url"),
                "title": (dom.get("title") or "")[:160],
                "platform": detect_platform(dom, names),
                "cloudflare": {"blocked": bool(markers), "markers": markers},
                "page_kind": classify_page(dom.get("url", ""), dom),
                "mage_cache_storage": dom.get("hasMageCacheStorage"),
                "add_to_cart_controls": dom.get("addToCartControls"),
                "consent_overlay_visible": bool(dom.get("consentBlockers")),
                "captcha_visible": dom.get("captchaVisible"),
                "text_excerpt": (dom.get("visibleText") or "")[:500],
            }
            return json.dumps(out, ensure_ascii=False)
        finally:
            with contextlib.suppress(Exception):
                await browser.close()
            with contextlib.suppress(Exception):
                await pw.stop()

    @tools.action(
        "Dismiss cookie banners / consent managers / popups covering the page: looks in the page and in the shadow "
        "DOM of Usercentrics, Cookiebot, CookieFirst, Didomi, eRecht24 for an accept button ('Accept all', "
        "'Alle akzeptieren', 'Alles akzeptieren', 'Ich stimme zu', …), clicks it and hides a leftover overlay. "
        "Returns which button was clicked."
    )
    async def dismiss_overlays() -> str:
        scope = SCOPE
        pw, browser = await connect_over_cdp(scope.cdp_url)
        try:
            page = await page_for_host(browser, scope.host) if scope.host else None
            if page is None:
                return json.dumps({"error": f"no open tab for {scope.host or 'the current shop'}"})
            clicked = await evaluate_safely(page, DISMISS_OVERLAYS_JS) or {"clicked": None}
            if clicked.get("clicked"):
                await asyncio.sleep(0.8)
                clicked["cleanup"] = await evaluate_safely(page, REMOVE_COVER_JS)
            elif (await evaluate_safely(page, REMOVE_COVER_JS) or {}).get("removed"):
                clicked = {"clicked": None, "hid_leftover": True}
            return json.dumps(clicked, ensure_ascii=False)
        finally:
            with contextlib.suppress(Exception):
                await browser.close()
            with contextlib.suppress(Exception):
                await pw.stop()

    @tools.action(
        "List the product cards visible on the current page (index + name). Use the index with open_product_card."
    )
    async def list_product_cards() -> str:
        scope = SCOPE
        pw, browser = await connect_over_cdp(scope.cdp_url)
        try:
            page = await page_for_host(browser, scope.host) if scope.host else None
            if page is None:
                return json.dumps({"error": f"no open tab for {scope.host or 'the current shop'}"})
            cards = await evaluate_safely(page, PRODUCT_CARDS_JS) or []
            return json.dumps({"count": len(cards), "cards": [{"index": i, "name": c.get("name", "")} for i, c in enumerate(cards)]}, ensure_ascii=False)
        finally:
            with contextlib.suppress(Exception):
                await browser.close()
            with contextlib.suppress(Exception):
                await pw.stop()

    @tools.action(
        "Open the product card with this index (from list_product_cards) by clicking it. If the card only offers an "
        "add-to-cart button (no product page), that button is clicked instead. Returns the resulting URL."
    )
    async def open_product_card(index: int = 0) -> str:
        scope = SCOPE
        pw, browser = await connect_over_cdp(scope.cdp_url)
        try:
            page = await page_for_host(browser, scope.host) if scope.host else None
            if page is None:
                return json.dumps({"error": f"no open tab for {scope.host or 'the current shop'}"})
            result = await evaluate_safely(page, OPEN_CARD_JS, index) or {}
            await asyncio.sleep(2.0)
            return json.dumps({**result, "url_now": getattr(page, "url", "")}, ensure_ascii=False)
        finally:
            with contextlib.suppress(Exception):
                await browser.close()
            with contextlib.suppress(Exception):
                await pw.stop()

    @tools.action(
        "Open a URL in the current shop tab and return the resulting URL, title and page kind. Use it whenever the "
        "agent REPL reports about:blank, or to go straight to a shop page (product, cart, checkout) after clicking "
        "opened the page in a tab the REPL is not attached to. Only URLs on the shop under test are accepted."
    )
    async def goto_url(url: str) -> str:
        scope = SCOPE
        target = url.strip()
        if not host_matches(target, scope.host):
            return json.dumps({"error": f"only {scope.host or 'the shop under test'} is reachable from here", "url": target}, ensure_ascii=False)
        pw, browser = await connect_over_cdp(scope.cdp_url)
        try:
            page = await page_for_host(browser, scope.host)
            if page is None:
                return json.dumps({"error": "no open tab for the shop"})
            try:
                response = await page.goto(target, wait_until="domcontentloaded", timeout=45_000)
                await asyncio.sleep(1.5)
                dom = await evaluate_safely(page, DOM_INFO_JS) or {}
                return json.dumps(
                    {
                        "url": getattr(page, "url", target),
                        "http_status": response.status if response else None,
                        "title": (dom.get("title") or "")[:140],
                        "page_kind": classify_page(getattr(page, "url", target), dom),
                        "add_to_cart_controls": dom.get("addToCartControls"),
                        "mage_cache_storage": dom.get("hasMageCacheStorage"),
                    },
                    ensure_ascii=False,
                )
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"error": f"{type(exc).__name__}: {exc}"[:300], "url": target}, ensure_ascii=False)
        finally:
            with contextlib.suppress(Exception):
                await browser.close()
            with contextlib.suppress(Exception):
                await pw.stop()

    return tools


def model_ref(provider: str, model: str) -> str:
    """The model reference shown to Browser-Use / pydantic-ai for this run."""
    if provider == "deepseek":
        return "deepseek:" + DEEPSEEK_MODEL_ALIASES.get(model, model)
    if provider == "openai":
        return "openai:" + model
    if provider == "anthropic":
        return "anthropic:" + model
    return "google:" + model


def resolve_provider(args: argparse.Namespace) -> str:
    if args.llm != "auto":
        return args.llm
    if os.environ.get("DEEPSEEK_API_KEY"):
        return "deepseek"
    LOG.warning("DEEPSEEK_API_KEY is not set; --llm auto falls back to DeepSeek and will fail. Pass --llm explicitly.")
    return "deepseek"


def configure_browser_paths() -> None:
    """Make Playwright find a Chromium even when its default install dir is not writable/installed.

    Order: an explicit PLAYWRIGHT_BROWSERS_PATH, then a checkout-local
    ``.playwright-browsers`` directory, else Playwright's default. This is what
    makes ``pip install -r requirements.txt`` + a local browsers dir enough.
    """
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return
    local = Path(".playwright-browsers")
    if local.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local.resolve())
        LOG.debug("using browsers from %s", local.resolve())


def configure_browser_harness_home() -> Path:
    """Keep browser-use's own state directory writable.

    Browser-Use 1.0 spawns a browser-harness daemon that writes to
    ``$BH_HOME`` (default ``~/.config/browser-harness``). On machines where that
    is read-only the run dies at the first step, so we point it at a local dir.
    """
    if os.environ.get("BH_HOME"):
        return Path(os.environ["BH_HOME"])
    home = Path.home() / ".config" / "browser-harness"
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return home
    except OSError:
        local = Path(".browser-harness").resolve()
        local.mkdir(parents=True, exist_ok=True)
        os.environ["BH_HOME"] = str(local)
        LOG.info("~/.config/browser-harness is not writable; using %s for browser-use state", local)
        return local


def resolve_llm(args: argparse.Namespace) -> Any:
    """Turn --llm/--model/--api-key into what ``Agent(llm=...)`` accepts.

    DeepSeek goes through pydantic-ai's ``deepseek:<model>`` reference rather than
    ``ChatOpenAI(base_url=...)``: the provider knows DeepSeek's thinking mode
    rejects a forced tool_choice, and the reference travels the way 1.0 expects.
    """
    provider = resolve_provider(args)
    env_name = PROVIDER_KEYS[provider]
    api_key = args.api_key or os.environ.get(env_name)
    if not api_key:
        raise SystemExit(f"{env_name} is not set: put it in .env (see .env.example), export it, or pass --api-key.")
    os.environ[env_name] = api_key  # the pydantic provider reads it without showing it in the process list
    model = args.model or {
        "deepseek": "deepseek-chat",
        "openai": DEFAULT_OPENAI_MODEL,
        "anthropic": DEFAULT_ANTHROPIC_MODEL,
        "google": DEFAULT_GOOGLE_MODEL,
    }[provider]
    args.resolved_provider = provider
    return model_ref(provider, model)


TASK_PROMPT = """\
You are operating a real browser on the shop {url}. Your job: put one product in the cart, prove it is there,
and stop on the checkout page's delivery address form.

1. DETECT & INSPECT — look, don't assume. Call the inspect_shop_page tool (or read the page) to determine:
   - the shop platform: Magento, or Shopify / WooCommerce / Shopware / JTL / something else;
   - whether Cloudflare is protecting the site: a "Just a moment…" / "Nur einen Moment…" challenge, a CAPTCHA /
     Turnstile widget, an HTTP 403 from Cloudflare;
   - what kind of page you landed on: homepage, category grid, product page, search / CMS / cart / error page.
   If Cloudflare blocks you: finish immediately with the reason "blocked by Cloudflare". Do not retry, do not try
   to get around it.

2. OVERLAYS — dismiss cookie banners and consent managers (Usercentrics, Cookiebot, CookieFirst, eRecht24),
   newsletter popups, age/region gates, chat widgets. Prefer "Accept all" / "Alle akzeptieren" /
   "Alles akzeptieren" / "Ich stimme zu" — the dismiss_overlays tool does exactly that. Re-check after EVERY
   navigation: an overlay that covers the Add-to-Cart button swallows the click. A CAPTCHA or a login wall you
   cannot get past is a blocker: finish and report it.

3. ADD TO CART — on a product page click its "Add to Cart" / "In den Warenkorb" / "Zum Warenkorb hinzufügen".
   On a listing or the homepage open a product first (list_product_cards + open_product_card, or click a tile;
   if the click opens a new tab, continue with goto_url on the product URL in the current tab).
   On Magento listing pages the add button is inside a small form: click it directly; if the click is refused
   because a quantity is required, set that row's ANZAHL/quantity to 1 and click again. Select any required
   options (size, color, …) before adding. If a click seems to do nothing, call verify_magento_cart to see
   whether it worked before clicking again.

4. VERIFY — call the tool verify_magento_cart AS A TOOL CALL (a tool/function call — never as Python code:
   typing verify_magento_cart() into a python cell only gives NameError). Never invent its answer. Only continue
   once it returns in_cart: true. If the platform is not Magento it will legitimately return in_cart: false
   (no mage-cache-storage): then report the platform and finish — do not fabricate a Magento cart.

5. CHECKOUT — open the cart (cart icon or "Warenkorb"), then "Proceed to Checkout" / "Zur Kasse" / "Checkout" /
   "Kasse". If the cart hides that button behind an accordion ("Geschätzte Steuern und Versandgebühren",
   "Estimated Tax"), expand it, or go to the shop's checkout page directly (goto_url("<shop>/checkout/"), which works
   for Magento, Shopify and most themes). Reach the step with the delivery / shipping address form (E-Mail,
   Vorname, Nachname, Adresse/street, PLZ/zip, Stadt/city, Land/country, phone).

Hard rules: never fill in the address form, never place an order, never enter personal data. As soon as the
delivery/shipping address form is visible, scroll it into view, leave the browser there and finish.

You have at most {max_steps} steps. If something fails twice, change approach (tool instead of click, another
product, direct URL) instead of repeating it. In your final answer state: platform; cloudflare (yes/no + why);
starting page kind; product added; what verify_magento_cart returned; where you ended (URL); what blocked you, if
anything.
"""

SYSTEM_EXTENSION = (
    "The shop under test is {url}. The agent REPL starts in its own fresh tab: whenever page_info() shows about:blank "
    "or a different site, call goto_url('{url}') (or your goto_url tool) before doing anything else, and never work "
    "against about:blank. Results of the python helpers (page_info, list_tabs, screenshot) describe only the tab the "
    "REPL is attached to."
)


def make_agent(llm: Any, browser: Any, tools: Any, task: str, url: str | None = None) -> Any:
    from browser_use import Agent

    return Agent(
        task=task,
        llm=llm,
        browser=browser,
        tools=tools or None,
        output_model_schema=str,
        extend_system_message=SYSTEM_EXTENSION.format(url=url) if url else None,
        # the judge runs one more (paid) call per URL: this script judges the outcome itself
        judge=False,
    )


NEGATIONS = re.compile(r"\b(not|no|n't|never|kein|keine|nicht|ohne)\b", re.I)


def _affirmative(text: str, pattern: str) -> bool:
    """True when ``pattern`` matches in a clause that is not negated right before it.

    ``"Cloudflare: no"`` or ``"could not reach the form"`` must not read as yes.
    """
    for m in re.finditer(pattern, text, re.I):
        clause = text[max(0, m.start() - 25): m.start()]
        if not NEGATIONS.search(clause):
            return True
    return False


def _explicit_in_cart(low: str) -> bool | None:
    """``in_cart: true`` / ``in_cart = false`` stated by the agent; None when it only described the cart in prose."""
    values = re.findall(r"in_cart[\"']?\s*[:=]\s*(true|false)", low)
    if not values:
        return None
    return values[-1] == "true"


def parse_agent_report(final: str | None) -> dict[str, Any]:
    """Best-effort structure of the agent's own answer (for the report, never for the verdict)."""
    text = final or ""
    low = text.lower()
    platform = None
    for name in ("magento", "shopify", "woocommerce", "shopware", "jtl", "sylius", "prestashop"):
        if re.search(rf"\b{name}\b", low):
            platform = name
            break
    urls = [u.rstrip(".,;:!?") for u in re.findall(r"https?://[^\s)\]\"']+", text)]
    return {
        "platform_claim": platform,
        "cloudflare_claim": _affirmative(text, r"cloudflare") and not re.search(
            r"(cloudflare[:\s-]*(no|none|false|nein)\b)|((no|not|nicht|kein)[^.]{0,20}cloudflare)", low
        ),
        # an explicit in_cart value wins over prose; prose counts only when no value is stated
        "in_cart_claim": _explicit_in_cart(low) if _explicit_in_cart(low) is not None else _affirmative(
            text, r"\bin_cart\b|in the cart|item (is|was) in|added to the cart"
        ),
        "reached_form_claim": _affirmative(text, r"delivery form|shipping (address )?form|address form|versandadresse|delivery address|lieferadresse")
        and not re.search(r"(did not|didn't|could not|couldn't|not) reach", low),
        "blocked_claim": _affirmative(text, r"captcha|login wall|blocked by|blocker"),
        "urls": urls[:5],
        "excerpt": re.sub(r"\s+", " ", text)[:1200],
    }


def agent_success(result: Any) -> bool | None:
    """The agent's own verdict, from either source the 1.0 result offers.

    ``result.success`` is only set when the agent called finish(..., success=…);
    when it did not, the built-in judge's verdict (``result.judgement.success``)
    is the one to use. None means "no verdict", never "success".
    """
    if result is None:
        return None
    success = getattr(result, "success", None)
    if isinstance(success, bool):
        return success
    judgement = getattr(result, "judgement", None)
    judged = getattr(judgement, "success", None)
    return judged if isinstance(judged, bool) else None


def agent_steps(result: Any) -> int | None:
    """Model requests of the run. In 1.0 ``result.steps`` is an int (it was a list in 0.x)."""
    steps = getattr(result, "steps", None)
    if isinstance(steps, int) and not isinstance(steps, bool):
        return steps or None
    if isinstance(steps, (list, tuple)):
        return len(steps) or None
    counter = getattr(result, "number_of_steps", None)
    if callable(counter):
        try:
            return int(counter()) or None
        except Exception:  # noqa: BLE001
            return None
    return None


class CheckoutWatcher:
    """Watch our own tab while the agent works.

    As soon as the delivery form shows up (checkout URL + street/city/zip
    fields) the page is scrolled to it and frozen, so the form stays on screen
    for the screenshot and the agent's final steps.
    """

    URL_HINT = re.compile(r"checkout|onepage|kasse|cassa", re.I)

    def __init__(self, context: Any, interval: float = 2.0):
        self.context = context
        self.interval = interval
        self.found: dict[str, Any] | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="checkout-watcher")

    async def _loop(self) -> None:
        try:
            while self.found is None:
                await asyncio.sleep(self.interval)
                await self.scan()
        except asyncio.CancelledError:
            return

    async def scan(self) -> dict[str, Any] | None:
        for page in self.context.pages:
            if page.is_closed():
                continue
            data = await evaluate_safely(page, CHECKOUT_SCAN_JS)
            if not data or not data.get("hasForm") or not self.URL_HINT.search(data.get("url") or ""):
                continue
            self.found = data
            await freeze_on_form(page)
            return data
        return None

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task


async def freeze_on_form(page: Any) -> None:
    """Keep the checkout form on screen while the screenshot and the report happen."""
    await evaluate_safely(
        page,
        """() => {
          const vis = (e) => !!(e.offsetWidth || e.offsetHeight);
          const f = [...document.querySelectorAll('input, select')].find((e) => vis(e) && /street|adresse|strasse/i.test((e.name || '') + (e.id || '')))
            || [...document.querySelectorAll('input, select')].find(vis);
          if (f) { f.scrollIntoView({ block: 'start', behavior: 'instant' }); window.scrollBy(0, -100); }
          window.__magentoAgentFreezeTop = window.scrollY;
          document.querySelectorAll('video').forEach((v) => { try { v.pause(); } catch (e) {} });
          if (!window.__magentoAgentFreezeTimer) {
            window.__magentoAgentFreezeTimer = setInterval(() => {
              if (Math.abs(window.scrollY - window.__magentoAgentFreezeTop) > 8) window.scrollTo({ top: window.__magentoAgentFreezeTop });
            }, 800);
          }
        }""",
    )
    LOG.info("delivery form detected — page frozen on it")


async def run_agent(task: str, llm: Any, browser: Any, tools: Any, context: Any, *, max_steps: int, timeout: float, url: str | None = None) -> tuple[Any, CheckoutWatcher]:
    agent = make_agent(llm, browser, tools, task, url=url)
    watcher = CheckoutWatcher(context)
    watcher.start()
    try:
        result = await agent.run(max_steps=max_steps, timeout=timeout)
    finally:
        await watcher.stop()
        await _calm_blanks(context)
    return result, watcher


async def _calm_blanks(context: Any) -> None:
    """After a run, a leftover blank tab without a title is one the browser opened (never one that had content):
    close it so the next URL and the screenshot step find the shop tab predictably."""
    keep = [p for p in context.pages if not p.is_closed() and p.url != "about:blank"]
    for page in list(context.pages):
        try:
            if page.is_closed() or page.url != "about:blank":
                continue
            if not keep:  # never leave the browser with no tab at all
                break
            if await page.title():
                continue
            await page.close()
            LOG.debug("closed a leftover blank tab")
        except Exception:  # noqa: BLE001
            continue


async def find_checkout_page(browser: Any, host: str) -> Any | None:
    pages = pages_of(browser)
    same = [p for p in pages if host_matches(getattr(p, "url", ""), host)]
    ordered = same + [p for p in pages if p not in same]
    pattern = re.compile(r"checkout|onepage|kasse|cassa", re.I)
    for page in ordered:
        if pattern.search(getattr(page, "url", "")):
            return page
    for page in ordered:  # single-page checkout: trust the address form itself
        data = await evaluate_safely(page, CHECKOUT_SCAN_JS)
        if data and data.get("hasForm"):
            return page
    return None


async def capture_checkout(cdp_url: str, host: str, out_path: Path, *, attempts: int = 1, wait: float = 3.0) -> tuple[str, str | None]:
    """Screenshot the checkout form with our own CDP connection: (path, page url)."""
    last: Exception | None = None
    for attempt in range(max(1, attempts)):
        pw, browser = await connect_over_cdp(cdp_url)
        try:
            page = await find_checkout_page(browser, host)
            if page is not None:
                await freeze_on_form(page)
                await asyncio.sleep(min(wait, 2.0))
                await asyncio.sleep(0.2)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(out_path), full_page=False)
                return str(out_path), getattr(page, "url", None)
            last = RuntimeError("no checkout page found in the browser")
        except Exception as exc:  # noqa: BLE001
            last = exc
        finally:
            with contextlib.suppress(Exception):
                await browser.close()
            with contextlib.suppress(Exception):
                await pw.stop()
        if attempt + 1 < attempts:
            await asyncio.sleep(wait)
    raise last or RuntimeError("screenshot failed")


def cloudflare_verdict(status: int | None, server: str | None, dom: dict[str, Any] | None, cookies: list[dict[str, Any]]) -> dict[str, Any]:
    names = [c.get("name", "") for c in cookies]
    markers = detect_cloudflare(
        http_status=status,
        server_header=server,
        title=(dom or {}).get("title", ""),
        html=(dom or {}).get("html", ""),
        visible_text=(dom or {}).get("visibleText", ""),
        cookie_names=names,
        dom=dom,
    )
    return {"blocked": bool(markers), "markers": markers}


def decide_status(*, cf: dict[str, Any], platform: str, ground: dict[str, Any], report: dict[str, Any], reached_form: bool) -> str:
    if cf.get("blocked"):
        return "cloudflare_blocked"
    if ground.get("in_cart"):
        return "in_cart" if reached_form else "in_cart_no_checkout"
    if platform in ("shopify", "woocommerce", "shopware", "jtl", "sylius", "prestashop", "hybris"):
        return "platform_mismatch"
    if report.get("blocked_claim"):
        return "blocked_by_agent"
    return "failed"


async def process_url(url: str, index: int, args: argparse.Namespace, llm: Any, session: BrowserSession | None = None) -> tuple[UrlResult, BrowserSession | None]:
    """Launch a browser for this URL, let the agent drive it, then verify and clean up.

    Returns the result and (when the caller asked to keep it) the browser that
    still shows the checkout form.
    """
    from browser_use import Browser as BU_Browser
    from browser_use import __version__ as bu_version

    started = time.monotonic()
    res = UrlResult(url=url)
    set_current_url(url)
    host = host_of(url)
    shot_dir = Path(args.screenshot_dir)
    shot_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(url, index)
    own_session = session is None
    bu_browser: BU_Browser | None = None
    ground: dict[str, Any] = {}
    report: dict[str, Any] = {}
    cf: dict[str, Any] = {"blocked": False, "markers": []}
    platform = "unknown"
    try:
        if session is None:
            # one CDP port per URL: launching is serialized across the whole run
            session = await launch_browser(_launch_options(args))
        page = session.page
        LOG.info("[%s] Chromium up, CDP at %s (browser-use %s over the same browser)", url, session.cdp_url, bu_version)

        status: int | None = None
        server = ""
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            status = response.status if response else None
            server = (response.headers or {}).get("server", "") if response else ""
        except Exception as exc:  # noqa: BLE001
            LOG.warning("[%s] initial navigation failed: %s", url, exc)
        await asyncio.sleep(1.5)
        cookies = await cookies_of(session.context)
        dom = await evaluate_safely(page, DOM_INFO_JS) or {}
        cf = cloudflare_verdict(status, server, dom, cookies)
        platform = detect_platform(dom, [c.get("name", "") for c in cookies])
        if cf["blocked"]:  # a challenge page says nothing about the shop behind it
            platform = "unknown"
        res.platform, res.page_kind = platform, classify_page(getattr(page, "url", url), dom)
        res.cf_markers = cf["markers"]
        LOG.info("[%s] landing: HTTP %s server=%s platform=%s page=%s cloudflare=%s", url, status, server or "-", platform, res.page_kind, "; ".join(cf["markers"]) or "no")
        try:
            landing = shot_dir / f"{stem}_landing.png"
            await page.screenshot(path=str(landing))
            res.screenshots["landing"] = str(landing)
        except Exception:  # noqa: BLE001
            pass

        if cf["blocked"]:
            res.cloudflare = True
            res.status = "cloudflare_blocked"
            res.error = "Cloudflare blocks this URL: " + "; ".join(cf["markers"])
            LOG.warning("[%s] Cloudflare block recorded (%s); the agent is not started", url, "; ".join(cf["markers"]))
            return res, (session if not own_session else None)

        bu_browser = BU_Browser(cdp_url=session.cdp_url, keep_alive=True)
        SCOPE.cdp_url = session.cdp_url
        tools = build_tools()
        task = TASK_PROMPT.format(url=url, max_steps=args.max_steps)
        LOG.info("[%s] agent starting (max_steps=%s, timeout=%ss)", url, args.max_steps, args.timeout)
        result, watcher = await run_agent(task, llm, bu_browser, tools, session.context, max_steps=args.max_steps, timeout=args.timeout, url=url)
        final = result.final_result() if result is not None else None
        res.final_result = (final or "")[:4000] or None
        res.agent_success = agent_success(result)
        res.steps_used = agent_steps(result)
        if result is not None and getattr(result, "error", None):
            res.error = str(result.error)[:1000]
            LOG.warning("[%s] agent ended with: %s", url, res.error)
        report = parse_agent_report(final)
        res.agent_reported = report
        LOG.info("[%s] agent finished: success=%s status=%s steps=%s", url, res.agent_success, getattr(result, "status", None), res.steps_used)

        ground = await _safe_step(url, "ground-truth cart read", lambda: read_magento_cart(session.cdp_url, url), res)
        ground = ground or {"in_cart": False, "found": False, "summary_count": 0, "items_count": 0, "items": [], "error": "cart read failed"}
        try:
            cookies = await cookies_of(session.context)
            dom_now = None
            for p in session.context.pages:
                if not p.is_closed() and host_matches(p.url, host):
                    dom_now = await evaluate_safely(p, DOM_INFO_JS) or dom_now
            if dom_now:
                platform = detect_platform(dom_now, [c.get("name", "") for c in cookies])
                cf_after = cloudflare_verdict(None, None, dom_now, cookies)
                if cf_after["blocked"]:
                    cf = cf_after
        except Exception as exc:  # noqa: BLE001
            LOG.warning("[%s] post-run page re-read failed: %s", url, exc)
        LOG.info("[%s] ground truth: in_cart=%s summary_count=%s platform=%s", url, ground.get("in_cart"), ground.get("summary_count"), platform)

        reached_form = bool(watcher.found)
        if not reached_form:
            try:
                reached_form = bool(await watcher.scan())
            except Exception as exc:  # noqa: BLE001
                LOG.warning("[%s] final checkout scan failed: %s", url, exc)
        res.checkout_url = (watcher.found or {}).get("url")

        res.cart, res.platform, res.cloudflare, res.cf_markers = ground, platform, cf["blocked"], cf["markers"]
        res.status = decide_status(cf=cf, platform=platform, ground=ground, report=report, reached_form=reached_form)

        if platform == "unknown" and not cf["blocked"]:
            LOG.warning("[%s] platform could not be identified; leaving it for the report rather than guessing", url)

        wants_shot = res.status in ("in_cart", "in_cart_no_checkout", "platform_mismatch")
        if wants_shot:
            path = shot_dir / f"{stem}.png"

            async def _shot() -> tuple[str, str | None]:
                return await capture_checkout(session.cdp_url, host, path, attempts=args.checkout_retries, wait=args.checkout_wait)

            shot = await _safe_step(url, "checkout screenshot", _shot, res)
            if shot:
                res.screenshot, res.checkout_url = shot[0], shot[1] or res.checkout_url
                res.screenshots["checkout"] = shot[0]
                if not reached_form:  # the page itself proves the form is there
                    reached_form = True
                    res.status = decide_status(cf=cf, platform=platform, ground=ground, report=report, reached_form=True)
            else:
                with contextlib.suppress(Exception):
                    final_shot = shot_dir / f"{stem}_final.png"
                    await page.screenshot(path=str(final_shot))
                    res.screenshots["final"] = str(final_shot)
        LOG.info("[%s] -> %s%s", url, res.status, f" (screenshot {res.screenshot})" if res.screenshot else "")
        return res, (session if not own_session else None)
    except Exception as exc:  # noqa: BLE001
        LOG.exception("[%s] run failed", url)
        res.error = f"{type(exc).__name__}: {exc}"[:1500]
        res.status = "failed"
        res.platform = None if platform == "unknown" else platform
        res.cloudflare, res.cf_markers = bool(cf.get("blocked")), cf.get("markers", [])
        return res, (session if not own_session and session is not None else None)
    finally:
        res.duration_s = round(time.monotonic() - started, 1)
        if session is not None and own_session:
            if bu_browser is not None:
                with contextlib.suppress(Exception):
                    await bu_browser.stop()  # keep_alive: stops Browser-Use's side only
            await session.close()


async def _safe_step(url: str, what: str, step: Any, res: UrlResult) -> Any:
    """Run one post-processing step; a failure is recorded on the result, never fatal for the run."""
    try:
        return await step()
    except Exception as exc:  # noqa: BLE001
        LOG.warning("[%s] %s failed: %s: %s", url, what, type(exc).__name__, exc)
        note = f"{what}: {type(exc).__name__}: {exc}"[:400]
        res.error = f"{res.error}; {note}" if res.error else note
        return None


def _launch_options(args: argparse.Namespace) -> LaunchOptions:
    return LaunchOptions(
        cdp_port=args.cdp_port,
        headless=args.headless,
        profile_dir=args.profile_dir,
        channel=args.channel,
        executable_path=args.executable_path,
        proxy=args.proxy or None,
        user_agent=args.user_agent or None,
        cookies_file=args.cookies or None,
        stealth_js=args.stealth_js or None,
    )


def build_parser() -> argparse.ArgumentParser:
    env = {k: os.environ.get("MAGENTO_" + k.upper(), v) for k, v in MAGENTO_ENV_DEFAULTS.items()}
    p = argparse.ArgumentParser(
        prog="magento_cart_agent.py",
        description="AI browser agent: add a Magento product to cart and screenshot the checkout address form.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--urls", default="urls.txt", help="file with one URL per line (or a single URL)")
    p.add_argument("--llm", default="auto", choices=["auto", "deepseek", "openai", "anthropic", "google"], help="LLM provider")
    p.add_argument("--model", default=env["model"] or "deepseek-chat", help="model name (DeepSeek: the 'deepseek:<model>' reference)")
    p.add_argument("--api-key", default=None, help="API key (default: <PROVIDER>_API_KEY, e.g. DEEPSEEK_API_KEY)")
    p.add_argument("--env-file", default=".env", help="dotenv file, loaded before the other defaults are read")
    p.add_argument("--cdp-port", type=int, default=int(env["cdp_port"] or 9222), help="remote debugging port")
    p.add_argument("--profile-dir", default=env["profile_dir"] or None, help="persistent profile (default: throwaway temp dir)")
    p.add_argument("--channel", default=env["channel"] or None, choices=["chrome", "msedge", "chromium"], help="drive the installed browser")
    p.add_argument("--executable-path", default=None, help="explicit browser binary")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--headless", dest="headless", action="store_true", help="no window (Cloudflare fingerprints it more)")
    g.add_argument("--no-headless", dest="headless", action="store_false", help="show the browser window")
    p.set_defaults(headless=str(env["headless"]).lower() in ("1", "true", "yes"))
    p.add_argument("--proxy", default=env["proxy"] or None, help="scheme://[user:pass@]host[:port]")
    p.add_argument("--user-agent", default=env["user_agent"] or None)
    p.add_argument("--cookies", default=env["cookies"] or None, help="JSON file with cookies for context.add_cookies()")
    p.add_argument("--stealth-js", default=None, help="JS file used INSTEAD of the built-in stealth script")
    p.add_argument("--max-steps", type=int, default=int(env["max_steps"] or 40), help="model steps per URL")
    p.add_argument("--timeout", type=float, default=float(env["timeout"] or 900), help="seconds per agent run")
    p.add_argument("--checkout-wait", type=float, default=float(env["checkout_wait"] or 20), help="seconds to wait between screenshot retries")
    p.add_argument("--checkout-retries", type=int, default=2, help="attempts to screenshot the checkout form")
    p.add_argument("--stop-on-success", action="store_true", help="stop after the first verified cart")
    p.add_argument("--screenshot-dir", default=env["screenshot_dir"] or "checkout_screenshots")
    p.add_argument("--output", default=env["output"] or "results.json")
    p.add_argument("--list-tools", action="store_true", help="build the agent against the offline test model and print it")
    p.add_argument("--detect-only", action="store_true", help="launch, load each URL and record platform/Cloudflare only (no LLM, no key)")
    return p


def load_env_before_defaults(argv: list[str] | None = None) -> str:
    """--env-file must be applied before argparse reads env-var defaults: pre-parse it."""
    from dotenv import load_dotenv

    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env-file", default=".env")
    known, _ = pre.parse_known_args(argv)
    if Path(known.env_file).is_file():
        load_dotenv(known.env_file, override=False)
    return known.env_file


def _list_tools(args: argparse.Namespace) -> int:
    from pydantic_ai.models.test import TestModel

    from browser_use import Browser

    tools = build_tools()
    names = sorted(t.name for t in tools.functions)
    agent = make_agent(TestModel(), Browser(), tools, "offline listing")
    installed = [t for t in names if t not in ("python",)]
    print(f"custom tools ({len(installed)}): {', '.join(installed)}")
    for t in tools.functions:
        first = (t.description or "").strip().split(".")[0]
        print(f"  - {t.name}: {first}.")
    print(f"model reference: {model_ref(resolve_provider(args), args.model)}")
    print(f"agent object: {type(agent).__name__} ({type(agent.llm).__name__} on browser-use 1.0.6)")
    print(f"agent built against the offline TestModel: {type(agent).__name__} (judge off, output=str)")
    print(f"task prompt: {len(TASK_PROMPT)} chars")
    return 0


async def detect_only(urls: list[str], args: argparse.Namespace) -> int:
    """Load every URL in our own browser and record what the page says — without any agent."""
    results: list[UrlResult] = []
    for i, url in enumerate(urls, start=1):
        started = time.monotonic()
        res = UrlResult(url=url)
        session = None
        try:
            session = await launch_browser(_launch_options(args))
            response = await session.page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            status = response.status if response else None
            server = (response.headers or {}).get("server") if response else ""
            await asyncio.sleep(1.5)
            cookies = await cookies_of(session.context)
            dom = await evaluate_safely(session.page, DOM_INFO_JS) or {}
            cf = cloudflare_verdict(status, server, dom, cookies)
            platform = detect_platform(dom, [c.get("name", "") for c in cookies])
            if cf["blocked"]:  # a challenge page says nothing about the shop behind it
                platform = "unknown"
            res.platform, res.cf_markers, res.cloudflare = platform, cf["markers"], cf["blocked"]
            res.page_kind = classify_page(getattr(session.page, "url", url), dom)
            res.status = "cloudflare_blocked" if cf["blocked"] else "detected"
            landing = Path(args.screenshot_dir) / f"{_safe_name(url, i)}_landing.png"
            landing.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(Exception):
                await session.page.screenshot(path=str(landing))
                res.screenshots["landing"] = str(landing)
            LOG.info("[%s] HTTP %s platform=%s page=%s cloudflare=%s", url, status, platform, res.page_kind, "; ".join(cf["markers"]) or "no")
        except Exception as exc:  # noqa: BLE001
            res.error = f"{type(exc).__name__}: {exc}"[:500]
        finally:
            res.duration_s = round(time.monotonic() - started, 1)
            if session is not None:
                await session.close()
        results.append(res)
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "mode": "detect-only", "count": len(results),
           "results": [asdict(r) for r in results]}
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in results:
        LOG.info("  %-30s %-20s cf=%-5s platform=%-10s %s", r.url, r.status, r.cloudflare, r.platform, ", ".join(r.cf_markers))
    return 0


async def amain(argv: list[str] | None = None) -> int:
    load_env_before_defaults(argv)
    args = build_parser().parse_args(argv)

    if args.list_tools:
        return _list_tools(args)

    urls = resolve_urls(args.urls)
    if not urls:
        LOG.error("%r is neither an existing URL file nor a URL", args.urls)
        return 2
    configure_browser_harness_home()
    configure_browser_paths()
    if args.detect_only:  # smoke test of the launcher + detection: no key, no cost
        return await detect_only(urls, args)
    llm = resolve_llm(args)
    provider = resolve_provider(args)

    LOG.info("=== magento cart agent: %d URL(s) ===", len(urls))
    LOG.info("model=%s provider=%s headless=%s channel=%s profile=%s", model_ref(provider, args.model), provider, args.headless, args.channel or "-", args.profile_dir or "temp")
    results: list[UrlResult] = []
    for i, url in enumerate(urls, start=1):
        LOG.info("--- [%d/%d] %s", i, len(urls), url)
        res, _ = await process_url(url, i, args, llm)
        results.append(res)
        if args.stop_on_success and res.status == "in_cart":
            LOG.info("--stop-on-success: stopping after %s", url)
            break

    summary: dict[str, int] = {}
    for r in results:
        summary[r.status] = summary.get(r.status, 0) + 1
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model_ref(provider, args.model),
        "provider": provider,
        "browser_use": "1.0.6",
        "urls_file": str(args.urls),
        "count": len(results),
        "summary": summary,
        "results": [asdict(r) for r in results],
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    LOG.info("results written to %s", args.output)
    for r in results:
        LOG.info("  %-30s %-20s cf=%-5s platform=%-12s %s", r.url, r.status, r.cloudflare, r.platform, r.screenshot or "")
    return 0 if any(r.status == "in_cart" for r in results) else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("MAGENTO_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("browser_use", "playwright", "httpx", "httpcore", "asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        return asyncio.run(amain(argv))
    except KeyboardInterrupt:
        LOG.warning("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
