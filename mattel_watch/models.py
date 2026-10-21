from dataclasses import asdict, dataclass, field
from typing import Any
import re


@dataclass
class Variant:
    id: str
    title: str
    available: bool
    price_cents: int | None = None
    sku: str = ""


@dataclass
class Product:
    id: str
    handle: str
    title: str
    url: str
    groups: list[str]
    variants: list[Variant]
    image: str | None = None
    currency: str | None = None
    product_type: str = ""
    tags: list[str] = field(default_factory=list)
    launch_at: str | None = None
    launch_check: str = 'unchecked'
    launch_label: str | None = None
    ui_buyable: bool | None = None
    published_at: str | None = None
    created_at: str | None = None

    @property
    def available(self) -> bool:
        return any(v.available for v in self.variants)

    def selected_variant(self) -> Variant | None:
        # Never choose an arbitrary colour/size when more than one variant exists.
        return self.variants[0] if len(self.variants) == 1 and self.variants[0].available else None

    def eligible(self) -> bool:
        name = self.title.casefold()
        excluded = r"\b(membership|overdrive|t-shirt|tee|hoodie|snapback|hat|apparel|poster|gift card)\b"
        if not name.startswith("hot wheels") or re.search(excluded, name):
            return False
        if re.search(r"apparel|clothing|membership|accessories", self.product_type, re.I):
            return False
        # Curated collection membership, not fuzzy matches to all store products.
        return bool(set(self.groups) & {"rlc", "premium"})

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Product":
        raw = dict(raw)
        raw["variants"] = [Variant(**v) for v in raw["variants"]]
        return cls(**raw)
