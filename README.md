# AI browser agent — Magento cart → checkout form

One URL per line in, and for every URL the script launches **its own** Chromium
over CDP, hands that same browser to **[Browser-Use 1.0]** driven by **DeepSeek**,
lets the agent work out for itself what page it landed on and where "Add to Cart"
is, **verifies the cart independently** (`localStorage['mage-cache-storage']`),
goes through to the delivery/shipping address form of the checkout and
**screenshots it**. Cloudflare-protected sites are recorded as blocked instead of
being fought; non-Magento shops are recorded with their platform.


## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # or skip: use the installed browser with --channel chrome|msedge
cp .env.example .env                 # then put your DEEPSEEK_API_KEY in .env
```

The `DEEPSEEK_API_KEY` is read from `.env` (loaded *before* the CLI defaults are
computed) or from the environment.

> If `~/.cache/ms-playwright` is not writable on your machine, set
> `PLAYWRIGHT_BROWSERS_PATH=$PWD/.playwright-browsers` (the script also picks a
> local `.playwright-browsers/` directory up automatically when it exists).

## Run

```bash
python magento_cart_agent.py --urls urls.txt
```

Other things you will want:

```bash
# no LLM, no key, no cost: just load every URL and report platform + Cloudflare
python magento_cart_agent.py --urls urls.txt --detect-only

# installed Chrome instead of Playwright's Chromium (headful by default)
python magento_cart_agent.py --urls urls.txt --channel chrome

# your own proxy / cookies / profile, stop at the first success
python magento_cart_agent.py --urls urls.txt \
    --proxy http://user:pass@proxy.local:8080 --cookies cookies.json --profile-dir ./userdata \
    --stop-on-success --max-steps 60 --timeout 1200

# offline tool/prompt inspection (test model, no browser, no key)
python magento_cart_agent.py --list-tools
```

Every important flag has a `MAGENTO_*` environment variable (`MAGENTO_MODEL`,
`MAGENTO_CDP_PORT`, `MAGENTO_HEADLESS`, `MAGENTO_MAX_STEPS`, `MAGENTO_TIMEOUT`,
`MAGENTO_SCREENSHOT_DIR`, `MAGENTO_OUTPUT`, `MAGENTO_PROXY`, `MAGENTO_USER_AGENT`,
`MAGENTO_COOKIES`, `MAGENTO_PROFILE_DIR`, `MAGENTO_CHANNEL`) — flags win.
`--help` lists everything.

`--cookies` takes the same JSON as Playwright's `context.add_cookies()`: either a
list of cookie objects or `{"cookies": [...]}`.

## What the agent is told (and what it is checked against)

The task prompt makes the agent, per URL: **inspect** the page (platform,
Cloudflare, page kind) → **dismiss** consent/newsletter overlays → **add** a
product → **verify** with `verify_magento_cart` (a tool call, never Python
code) → **go to checkout** and stop on the delivery form without filling it.

There is no `if` on URL types in the code: the agent classifies the page. What
the script does by itself is *observe and verify*:

| Custom tool | Why it exists |
|---|---|
| `verify_magento_cart` | Reads `mage-cache-storage` over CDP, **scoped to the host under test**, so an old tab of a previous shop cannot leak its cart. Reports `in_cart`, `summary_count`, `items`, and the platform. |
| `inspect_shop_page` | Platform, Cloudflare markers, page kind, visible add-to-cart controls, consent overlay, text excerpt. |
| `dismiss_overlays` | Clicks "Accept all" / "Alle akzeptieren" / "Alles akzeptieren" / "Ich stimme zu" — including inside the shadow DOM of Usercentrics/Cookiebot/… — then hides a leftover cover. |
| `list_product_cards` / `open_product_card` | Turn an arbitrary listing into an index the model can act on by number (theme-agnostic: card markup **and** product-shaped links). |
| `goto_url` | Navigates the shop tab itself — the escape hatch when the agent REPL is attached to `about:blank`, when a click opened a new tab, or to jump straight to `/checkout/`. Only the host under test is accepted. |

After each run the script re-reads the cart itself. **The agent's verdict is
never ground truth.** Only that independent read (plus the delivery form being
on screen) decides `in_cart`.

## Results

`results.json`:

```json
{"url": "https://samenfest.de", "platform": "magento", "cloudflare": false,
 "status": "in_cart", "agent_success": true,
 "cart": {"in_cart": true, "found": true, "summary_count": 1, "items_count": 1,
          "items": ["Samenset Tomaten Basic"], "subtotal": 20.69},
 "screenshot": "checkout_screenshots/02_samenfest.de.png",
 "checkout_url": "https://samenfest.de/checkout/#shipping",
 "cf_markers": [], "page_kind": "homepage", "final_result": "…",
 "agent_reported": {"platform_claim": "magento", "in_cart_claim": true, …},
 "steps_used": 18, "duration_s": 142.3,
 "screenshots": {"landing": "…_landing.png", "checkout": "…png"}}
```

| status | meaning |
|---|---|
| `in_cart` | ground-truth cart has items **and** the delivery form was reached |
| `in_cart_no_checkout` | cart verified, delivery form not reached |
| `platform_mismatch` | non-Magento shop; a missing `mage-cache-storage` is expected, not a failed add |
| `cloudflare_blocked` | challenge / 403 / `__cf_bm` with a challenge — agent not started, markers recorded |
| `blocked_by_agent` | the agent hit a CAPTCHA or login wall inside the shop |
| `failed` | the agent did not finish, or the run raised |

## Anti-bot and platform notes

* **Cloudflare** is detected from markers: 403 + `Server: cloudflare`, titles
  ("Just a moment…" / "Nur einen Moment…"), challenge text, the
  `challenge-platform` script, the managed-challenge widget, `__cf_bm`. A live
  shop behind Cloudflare that merely *sets* `__cf_bm` is not treated as blocked.
  Detection happens before the agent starts, so a blocked URL costs no steps.
* **Platforms**: Magento (mage keys, `text/x-magento-init`, `/static/version…`,
  `require/ko`, Magento body classes), Shopify (`window.Shopify`,
  `cdn.shopify.com`, `cart_currency`), WooCommerce, Shopware, JTL, Sylius,
  PrestaShop, Hybris.
* **Consent overlays** really do swallow clicks (Usercentrics lives in a shadow
  DOM, eRecht24 paints a fixed layer); dismissing them is part of the flow and is
  re-checked after navigation.
* **Headful is the default**: headless Chromium is fingerprinted much more
  readily by Cloudflare. Use `--headless` for CI-like runs.

## DeepSeek

The model is passed as the pydantic-ai reference `deepseek:<model>`
(`--model deepseek-chat`, the default). This routes through pydantic-ai's
DeepSeek provider, which knows DeepSeek's thinking mode rejects a forced
`tool_choice` — a `ChatOpenAI(base_url=...)` would hit
`400 Thinking mode does not support this tool_choice`. The spec's
`deepseek-flash` is mapped to `deepseek-chat` (the API serves `deepseek-chat` and
`deepseek-reasoner`). `--llm openai|anthropic|google` switches providers.

## Tests

```bash
python -m pytest -q        # 48 tests, ~35 s, no API key, no network
```

* unit: URL normalization, `.env`-before-argparse, host scoping, platform/
  Cloudflare/page-kind heuristics, status decision table, agent-result parsing
  (`steps` is an int in 1.0.6 — a `len()` there once failed every run), negations
  in the agent's report;
* real browser against **local mock shops** (`tests/mock/serve.py`: Magento with
  a lazy listing and a quantity-required product, Shopify, a Cloudflare 403
  challenge, an overlay-heavy shop): the CDP bridge, `verify_magento_cart` via a
  real MCP tool call, consent dismissal, `goto_url` tab recovery after a
  `window.open()` click, leftover blank-tab cleanup, checkout watcher, screenshot,
  and one agent run with pydantic-ai's `TestModel` over CDP.

## Notes on the run

* The agent's REPL starts in its **own** tab (Browser-Use opens one when it attaches
  over CDP), so the system message tells it to `goto_url` the shop whenever the REPL
  shows `about:blank`; after each run a leftover untitled blank tab is closed.
* `judge=False` on the agent: it would spend one extra model call per URL on a
  verdict this script already computes itself (ground-truth cart + delivery form).
  `agent_success` in the results is therefore the agent's own `finish(success=…)`
  or `None` — never the judge.
* Post-processing steps (cart re-read, page re-read, screenshot) are isolated: a
  failing step is recorded in `error` but cannot overwrite the run's status.

## Troubleshooting

* `Executable doesn't exist` → `playwright install chromium`, or `--channel chrome`.
* `BrowserConfigError: … browser-harness dir … Read-only file system` → the script
  points `BH_HOME` at a local `.browser-harness/` automatically; set `BH_HOME`
  yourself to override.
* Port already in use → `--cdp-port 9333`.

[Browser-Use 1.0]: https://github.com/browser-use/browser-use
