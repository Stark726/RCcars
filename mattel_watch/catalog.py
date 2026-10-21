"""Read-only Shopify catalogue adapter. HTTP 403/challenges stop the scan."""
from urllib.parse import urlencode, urlsplit
import re
from .http import TransportError
from .models import Product, Variant
from .discovery import normalize_catalogue_time


class CatalogError(Exception):
    pass


def identifier(value):
    s = str(value)
    if not re.fullmatch(r"[0-9]+", s):
        raise CatalogError("Catalogue returned a non-numeric product/variant ID")
    return s


def image_url(raw):
    if isinstance(raw, dict):
        raw = raw.get("src")
    if not isinstance(raw, str):
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    parsed = urlsplit(raw)
    if parsed.scheme == "https" and parsed.hostname in {
        "images.mattel.net", "cdn.shopify.com", "creations.mattel.com"} and parsed.username is None and parsed.password is None:
        return raw
    return None


class ShopifyCatalog:
    PAGE_SIZE = 250

    def __init__(self, cfg, http):
        self.cfg, self.http = cfg, http

    def _get(self, url):
        try:
            value = self.http.request(url)
        except TransportError as exc:
            raise CatalogError(f"Mattel read failed ({exc}); no scan changes were committed. "
                               "403/429 may mean access is blocked or rate limited; do not bypass it.") from None
        return value

    def snapshot(self):
        found = {}
        for group, collection in self.cfg.collections.items():
            previous_ids = set()
            complete = False
            for page in range(1, self.cfg.max_pages + 1):
                query = urlencode({"limit": self.PAGE_SIZE, "page": page})
                payload = self._get(f"{self.cfg.store_url}/collections/{collection}/products.json?{query}")
                if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
                    raise CatalogError("Collection did not return a products array; baseline unchanged")
                rows = payload["products"]
                ids = {identifier(row.get("id")) for row in rows if isinstance(row, dict)}
                if rows and (not ids or ids <= previous_ids):
                    raise CatalogError("Catalogue pagination repeated a page; scan rejected")
                previous_ids |= ids
                for raw in rows:
                    product = self._parse(raw, group)
                    if product.id in found:
                        old = found[product.id]
                        product.groups = sorted(set(old.groups + product.groups))
                    found[product.id] = product
                if len(rows) < self.PAGE_SIZE:
                    complete = True
                    break
            if not complete:
                raise CatalogError("Collection exceeds max_pages; increase it before baselining")
            if not previous_ids:
                raise CatalogError(f"Collection {collection} was empty; refusing an empty/partial baseline")
        return list(found.values())

    def _parse(self, raw, group):
        try:
            if not isinstance(raw, dict) or not isinstance(raw.get("variants"), list) or not raw["variants"]:
                raise CatalogError("Product has no valid variants; scan rejected")
            handle = raw["handle"]
            if not isinstance(handle, str) or not re.fullmatch(r"[a-zA-Z0-9-]+", handle):
                raise CatalogError("Invalid product handle")
            variants = [Variant(identifier(v["id"]), str(v.get("title", "")),
                                v.get("available") is True, sku=str(v.get("sku") or ""))
                        for v in raw["variants"]]
            tags = raw.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",")]
            images = raw.get("images") or []
            return Product(identifier(raw["id"]), handle, str(raw["title"]),
                           f"{self.cfg.store_url}/products/{handle}", [group], variants,
                           image_url(images[0]) if images else None,
                           product_type=str(raw.get("product_type") or ""), tags=tags,
                           published_at=normalize_catalogue_time(raw.get('published_at')),
                           created_at=normalize_catalogue_time(raw.get('created_at')))
        except (KeyError, TypeError, IndexError):
            raise CatalogError("Malformed catalogue product; scan rejected") from None

    def details(self, product):
        # Ajax .js gives contextual prices in integer minor units. A collection's
        # products.json prices can be the shop's base currency: never label those CAD.
        raw = self._get(product.url + ".js")
        cart = self._get(self.cfg.store_url + "/cart.js")
        if not isinstance(raw, dict) or not isinstance(cart, dict):
            raise CatalogError("Malformed product/cart JSON")
        if identifier(raw.get("id")) != product.id:
            raise CatalogError("Product identity changed during refresh")
        if not isinstance(raw.get("variants"), list) or not raw["variants"]:
            raise CatalogError("Product details have no valid variants")
        currency = cart.get("currency")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise CatalogError("Cannot verify presentment currency")
        try:
            variants = []
            for v in raw["variants"]:
                if not isinstance(v, dict):
                    raise CatalogError("Malformed product variant")
                cents = v.get("price")
                if type(cents) is not int or cents < 0:
                    raise CatalogError("Product price must be integer minor units")
                variants.append(Variant(identifier(v["id"]), str(v.get("title", "")),
                                        v.get("available") is True, cents, str(v.get("sku") or "")))
            product.variants = variants
            product.currency = currency
            if raw.get("featured_image"):
                product.image = image_url(raw["featured_image"])
            return product
        except (KeyError, TypeError):
            raise CatalogError("Malformed product details") from None

    def watch_product(self, handle):
        if not re.fullmatch(r'[a-z0-9-]+', handle):
            raise CatalogError('Invalid watched handle')
        raw = self._get(f'{self.cfg.store_url}/products/{handle}.js')
        if not isinstance(raw, dict) or raw.get('handle') != handle:
            raise CatalogError('Watched product identity could not be verified')
        title = str(raw.get('title', ''))
        if re.search(r'\bRLC\b|Red Line Club', title, re.I):
            group = 'rlc'
        elif re.search(r'\bPremium\b|Car Culture|Boulevard|Team Transport', title, re.I):
            group = 'premium'
        else:
            raise CatalogError('Watched product is not explicitly identifiable as Hot Wheels RLC/Premium')
        product = self._parse(raw, group)
        if not product.eligible():
            raise CatalogError('Watched product is excluded by product filters')
        # details() independently verifies presentment prices and currency.
        return self.details(product)

    def refresh_watch(self, product):
        # Preserve known curated-collection membership for titles that do not
        # literally contain "Premium". details() rechecks the numeric product ID.
        return self.details(product)
