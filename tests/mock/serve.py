"""Local mock shops for offline tests: Magento (incl. a qty-only product and a lazy
listing), Shopify, a Cloudflare challenge, and an overlay-heavy shop.

Run standalone:  python tests/mock/serve.py [port]   (default 8765)
"""
from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PRODUCTS = {
    "1": ("Magento Mock Bag", "49,00"),
    "2": ("Magento Mock Socks", "9,50"),
    "3": ("Magento Qty Item", "12,00"),
}

MAGE_JS = """<script>
  const state = JSON.parse(localStorage.getItem('mage-cache-storage') || '{}');
  if (!state.cart) { state.cart = {summary_count: 0, items: [], subtotalAmount: 0}; localStorage.setItem('mage-cache-storage', JSON.stringify(state)); }
  function saveCart() { localStorage.setItem('mage-cache-storage', JSON.stringify(state)); }
  document.addEventListener('DOMContentLoaded', () => {
    saveCart();
    document.querySelectorAll('form[data-add]').forEach((f) => f.addEventListener('submit', (ev) => {
      ev.preventDefault();
      const id = f.dataset.add; const name = f.dataset.name; const price = parseFloat(f.dataset.price.replace(',', '.'));
      const qtyEl = f.querySelector('input[name=qty]');
      if (f.dataset.needsqty === '1' && (!qtyEl || !(parseInt(qtyEl.value, 10) > 0))) {
        const err = document.createElement('div'); err.className = 'message qty-error';
        err.textContent = 'Bitte spezifizieren Sie die Menge der Produkte.';
        f.appendChild(err); return;
      }
      state.cart.items.push({product_name: name, qty: 1});
      state.cart.summary_count = state.cart.items.length;
      state.cart.subtotalAmount = state.cart.items.reduce((a, i) => a + price, 0);
      saveCart();
      const box = document.createElement('div'); box.className = 'message'; box.style.cssText = 'position:fixed;top:10px;right:10px;background:#e8ffe8;border:1px solid #2a2;padding:8px;z-index:9999';
      box.textContent = 'Sie haben ' + name + ' zum Warenkorb hinzugefügt.'; document.body.appendChild(box);
    }));
  });
  function showCart() {
    const box = document.getElementById('cart-box');
    box.innerHTML = '<h3>Warenkorb</h3>';
    state.cart.items.forEach((i) => { const d = document.createElement('div'); d.textContent = i.product_name; box.appendChild(d); });
    const a = document.createElement('a'); a.href = '/checkout/'; a.className = 'action primary checkout'; a.textContent = 'Zur Kasse';
    box.appendChild(a);
  }
</script>"""


def page(title: str, body: str, script: str = "", extra_head: str = "", magento: bool = True) -> str:
    seed = init_js() if magento else ""
    return f"<!doctype html><html lang=de><head><meta charset=utf-8><title>{title}</title>{seed}{extra_head}</head><body class='cms-home cms-index-index page-layout-1column'>{body}{script}</body></html>"


def header() -> str:
    return (
        "<header><a href='/'>Shop</a> <a href='/catalog.html'>Katalog</a> "
        "<a href='/checkout/cart/' id=cart-link>Warenkorb <span id=count>0</span></a></header>"
    )


def init_js() -> str:
    """Bootstraps a per-tab cart seed once, so tabs of one origin do not overwrite each other."""
    return (
        "<script>if (sessionStorage.getItem('seed') === null) {"
        "sessionStorage.setItem('seed', '1');"
        "if (!localStorage.getItem('mage-cache-storage')) localStorage.setItem('mage-cache-storage', JSON.stringify({cart:{summary_count:0,items:[],subtotalAmount:0}}));"
        "}</script>"
    )


MAGENTO_HOME = page(
    "Mock Magento Shop",
    header() + "<h1>Willkommen</h1><a href='/catalog.html'>Zum Katalog</a>"
    "<div id=cart-box></div>",
    MAGE_JS + "<script type='text/x-magento-init'>{};</script><script src='/static/version123/frontend/Magento/mage.js'></script>",
)

MAGENTO_CATALOG = page(
    "Katalog",
    header() + "<h1>Katalog</h1>"
    '<form action="/checkout/cart/add/product/1/" data-add="1" data-name="Magento Mock Bag" data-price="49,00"><input type=hidden name=product value=1>'
    "<a href='/product/bag.html' class='product-item-link'>Magento Mock Bag</a> <button class='action tocart primary' type=submit>In den Warenkorb</button></form>"
    '<form action="/checkout/cart/add/product/2/" data-add="2" data-name="Magento Mock Socks" data-price="9,50">'
    "<a href='/product/socks.html' class='product-item-link'>Magento Mock Socks</a> <button class='action tocart primary' type=submit>In den Warenkorb</button></form>",
    MAGE_JS + "<script type='text/x-magento-init'>{};</script>",
)

MAGENTO_PRODUCT = page(
    "Magento Mock Bag",
    header() + "<div class='product-info-main'><h1>Magento Mock Bag</h1>"
    '<form action="/checkout/cart/add/product/1/" data-add="1" data-name="Magento Mock Bag" data-price="49,00">'
    "<label>Anzahl <input type=number name=qty value=1></label> <button id=product-addtocart-button class='action tocart primary' type=submit>In den Warenkorb</button></form></div>",
    MAGE_JS + "<script type='text/x-magento-init'>{};</script>",
)

MAGENTO_QTY_PRODUCT = page(
    "Magento Qty Item",
    header() + "<div class='product-info-main'><h1>Magento Qty Item</h1>"
    '<form action="/checkout/cart/add/product/3/" data-add="3" data-name="Magento Qty Item" data-price="12,00" data-needsqty="1">'
    "<label>ANZAHL <input type=number name=qty value=0></label> <button type=submit class=tocart>IN DEN WARENKORB</button></form></div>",
    MAGE_JS + "<script type='text/x-magento-init'>{};</script>",
)

MAGENTO_CART = page(
    "Warenkorb",
    header() + "<h1>Warenkorb</h1><div id=lines></div><button class='action primary checkout' onclick=\"location.href='/checkout/'\">Zur Kasse gehen</button>"
    "<script>const s=JSON.parse(localStorage.getItem('mage-cache-storage')||'{}');document.getElementById('lines').innerHTML=(s.cart? s.cart.items:[]).map(i=>i.product_name).join(', ');</script>",
    MAGE_JS,
)

MAGENTO_CHECKOUT = page(
    "Kasse",
    header() + "<h1>Versandadresse</h1><form action='/checkout/save' method=post>"
    "<label>E-Mail <input type=email name=email></label><label>Vorname <input name=firstname></label>"
    "<label>Nachname <input name=lastname></label><label>Straße <input name='street[0]'></label>"
    "<div style='height:1200px'></div>"
    "<label>PLZ <input name=postcode></label><label>Stadt <input name=city></label><label>Land <select name=country><option>Deutschland</option></select></label>"
    "</form><div class='step-title'>2. Versand</div>",
    MAGE_JS,
)

MAGENTO_LAZY_LISTING = page(
    "Katalog (lazy)",
    header() + "<h1>Katalog</h1><div id=tiles>Lade Produkte…</div>"
    "<script>setTimeout(() => { document.getElementById('tiles').innerHTML = "
    "'<div class=product-item><a class=\"product-item-link\" href=\"/product/bag.html\">Lazy Brand Product</a></div>'; }, 1200);</script>",
    MAGE_JS,
)

SHOPIFY_HOME = """<!doctype html><html><head><title>Shopify Mock</title><script>window.Shopify={theme:{}};window.__st={'id':'x'};</script></head>
<body><div class="shopify-section"><h1>Shopify Mock</h1><form action="/cart/add" id="add-form"><button type="submit">Add to cart</button></form></div>
<script>document.getElementById('add-form').addEventListener('submit',(e)=>{e.preventDefault();sessionStorage.setItem('cart_count','1');document.body.insertAdjacentHTML('beforeend','<div class=added>Item added</div>');});</script></body></html>"""

CF_CHALLENGE = """<!doctype html><html lang=de><head><title>Nur einen Moment…</title></head><body><noscript>Enable JavaScript</noscript>
<h1>semilac.de</h1><h2>Sicherheitsüberprüfung wird durchgeführt</h2><p>Diese Website nutzt einen Sicherheitsservice, um sich vor böswilligen Bots zu schützen.</p>
<div id="cf-chl-widget-abc_response"></div><script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script></body></html>"""

OVERLAY_HOME = page(
    "Overlay Shop",
    header()
    + '''<div id="uc" role="dialog" style="position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:5000">
          <div style="background:#fff;padding:20px;margin:80px auto;max-width:600px"><p>Diese Website verwendet Cookies.</p>
          <button id=uc-acc>Alle akzeptieren</button></div></div>
        <a href="/product/bag.html" class="product-item-link">Overlay Product</a>''',
    """<script>
      const host = document.createElement('div'); host.id = 'usercentrics-root'; host.setAttribute('role', 'dialog');
      host.style.cssText = 'position:fixed;inset:0;z-index:5000;background:rgba(0,0,0,.5)';
      document.body.appendChild(host);
      const shadow = host.attachShadow({mode: 'open'});
      shadow.innerHTML = '<div style="background:#fff;padding:20px"><p>Diese Website verwendet Cookies.</p><button id="accept">Alles akzeptieren</button></div>';
      shadow.getElementById('accept').addEventListener('click', () => { host.style.display = 'none'; document.getElementById('uc').style.display = 'none'; window.__accepted = true; });
    </script>""",
)

OVERLAY_PRODUCT = page(
    "Overlay Product",
    header()
    + '<div class="product-info-main"><h1>Overlay Product</h1>'
    '<form action="/padd" data-add="1" data-name="Overlay Product" data-price="5,00"><button type=submit class=tocart>In den Warenkorb</button></form></div>'
    + '''<div id="uc2" role="dialog" style="position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:5000"><div style="background:#fff;padding:20px"><p>Diese Website verwendet Cookies.</p><button id=acc>Alle akzeptieren</button></div></div>''',
    """<script>
      const a = document.getElementById('acc'); a.addEventListener('click', () => { document.getElementById('uc2').style.display = 'none'; });
      document.querySelector('form[data-add]').addEventListener('submit', (e) => { e.preventDefault();
        let st = JSON.parse(localStorage.getItem('mage-cache-storage') || '{"cart":{"summary_count":0,"items":[],"subtotalAmount":0}}');
        st.cart.items.push({product_name: 'Overlay Product', qty: 1}); st.cart.summary_count = st.cart.items.length;
        localStorage.setItem('mage-cache-storage', JSON.stringify(st)); });
    </script>
    <script type='text/x-magento-init'>{};</script>""",
)

POPUP_HOME = page(
    "Shop mit Popup",
    header()
    + """<a href="/product/bag.html" class="product-item-link" onclick="window.open(this.href); return false;">Popup Product</a>""",
    """<script>
      // a newsletter popup inside a custom element's shadow root, as real consent widgets do
      const host = document.createElement('newsletter-popup'); host.setAttribute('role', 'dialog');
      host.style.cssText = 'position:fixed;inset:0;z-index:6000;background:rgba(0,0,0,.6)';
      const shadow = host.attachShadow({mode: 'open'});
      shadow.innerHTML = '<div style="background:#fff;padding:20px"><p>Melden Sie sich an</p><button id="no">Nein danke</button></div>';
      shadow.getElementById('no').addEventListener('click', () => { host.style.display = 'none'; });
      document.body.appendChild(host);
    </script>""",
)

ROUTES = {
    "/": (200, MAGENTO_HOME),
    "/catalog.html": (200, MAGENTO_CATALOG),
    "/lazy.html": (200, MAGENTO_LAZY_LISTING),
    "/product/bag.html": (200, MAGENTO_PRODUCT),
    "/product/qty.html": (200, MAGENTO_QTY_PRODUCT),
    "/checkout/cart/": (200, MAGENTO_CART),
    "/checkout/": (200, MAGENTO_CHECKOUT),
    "/shopify/": (200, SHOPIFY_HOME),
    "/popup/": (200, POPUP_HOME),
    "/overlay/": (200, OVERLAY_HOME),
    "/overlay/product/bag.html": (200, OVERLAY_PRODUCT),
}
CF_BLOCKED_PATHS = {"/blocked/", "/blocked"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output clean
        pass

    def _send(self, status: int, body: str, headers: dict[str, str] | None = None) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path in CF_BLOCKED_PATHS:
            self._send(403, CF_CHALLENGE, {"Server": "cloudflare", "Set-Cookie": "__cf_bm=abc; Path=/; HttpOnly"})
            return
        if path.startswith("/static/"):
            self._send(200, "/* mage */", {"Content-Type": "text/javascript"})
            return
        if path.startswith("/cdn-cgi/"):
            self._send(200, "/* cf */", {"Content-Type": "text/javascript"})
            return
        if path == "/checkout/cart/":
            self._send(200, MAGENTO_CART)
            return
        route = ROUTES.get(path)
        if route:
            self._send(route[0], route[1])
            return
        self._send(404, page("Seite nicht gefunden", "<h1>404</h1>"))

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._send(200, page("ok", "ok"))


def start(port: int = 0) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


if __name__ == "__main__":
    srv, _, base = start(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
    print("mock shops on", base)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
