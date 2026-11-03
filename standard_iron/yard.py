"""Assign a yard of retired transformers to orders under three supply policies.

Bespoke reman restores the original nameplate, so only an exact match can fill
an order, and each unit keeps its own shop time. Nothing is stock.

Standard iron rebuilds onto a published interface. A shelf unit ships in shelf
weeks. Later builds of the same SKU use a learning curve that starts at half
the new-unit lead time. A core that cannot pass a factory test is not offered.

Orders are filled tightest need first. Each order takes the soonest remaining
offer that still arrives by its need week. Ties break on unit id.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Sku:
    id: str
    kva_mva: int
    hv_kv: str
    lv_kv: str
    z_min_pct: int
    z_max_pct: int
    new_weeks: int
    shelf_weeks: int
    first_build_weeks: int
    learn_weeks: int
    floor_weeks: int


@dataclass(frozen=True)
class Unit:
    id: str
    kva_mva: int
    hv_kv: str
    lv_kv: str
    z_pct: int
    failure: str
    shop_weeks: int
    on_shelf: bool
    interface_ok: bool


@dataclass(frozen=True)
class Order:
    id: str
    sku: str
    need_week: int


@dataclass(frozen=True)
class Yard:
    program: str
    recoverable: tuple[str, ...]
    guild_usd: dict[str, int]
    learn_discount_pct: int
    discount_cap_pct: int
    skus: tuple[Sku, ...]
    units: tuple[Unit, ...]
    orders: tuple[Order, ...]
    source: str

    def sku(self, sku_id: str) -> Sku:
        for item in self.skus:
            if item.id == sku_id:
                return item
        raise KeyError(sku_id)


@dataclass(frozen=True)
class Offer:
    unit_id: str
    sku_id: str
    weeks: int
    kind: str


@dataclass(frozen=True)
class Assignment:
    order_id: str
    sku_id: str
    need_week: int
    unit_id: str
    weeks: int
    kind: str


@dataclass(frozen=True)
class Miss:
    order_id: str
    sku_id: str
    need_week: int
    soonest_week: int | None


@dataclass(frozen=True)
class PolicyResult:
    name: str
    shipped: tuple[Assignment, ...]
    missed: tuple[Miss, ...]

    @property
    def shipped_count(self) -> int:
        return len(self.shipped)

    @property
    def missed_count(self) -> int:
        return len(self.missed)


@dataclass(frozen=True)
class Quote:
    failure: str
    seen: int
    guild_usd: int
    learned_usd: int | None
    quotable: bool


@dataclass(frozen=True)
class Report:
    yard: Yard
    policies: tuple[PolicyResult, ...]
    quotes: tuple[Quote, ...]

    def policy(self, name: str) -> PolicyResult:
        for item in self.policies:
            if item.name == name:
                return item
        raise KeyError(name)

    @property
    def result_line(self) -> str:
        return format_result_line(self)


def load_yard(path: Path) -> Yard:
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON ({exc.msg})") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected an object")
    where = str(path)
    recoverable = tuple(_need_str_list(raw, "recoverable", where))
    if not recoverable:
        raise ValueError(f"{where}: recoverable must not be empty")
    _unique(list(recoverable), f"{where}: duplicate failure name")
    guild = _load_guild(raw.get("guild_usd"), recoverable, where)
    skus = tuple(_load_sku(item, f"{where} skus") for item in _need_list(raw, "skus", where))
    if not skus:
        raise ValueError(f"{where}: skus must not be empty")
    _unique([sku.id for sku in skus], f"{where}: duplicate sku id")
    units = tuple(_load_unit(item, f"{where} units", guild) for item in _need_list(raw, "units", where))
    if not units:
        raise ValueError(f"{where}: units must not be empty")
    _unique([unit.id for unit in units], f"{where}: duplicate unit id")
    orders = tuple(_load_order(item, f"{where} orders") for item in _need_list(raw, "orders", where))
    if not orders:
        raise ValueError(f"{where}: orders must not be empty")
    _unique([order.id for order in orders], f"{where}: duplicate order id")
    known = {sku.id for sku in skus}
    for order in orders:
        if order.sku not in known:
            raise ValueError(f"{where}: order {order.id} sku {order.sku} is not published")
    return Yard(
        program=_need_str(raw, "program", where),
        recoverable=recoverable,
        guild_usd=guild,
        learn_discount_pct=_bounded_int(raw, "learn_discount_pct", where, 0, 100),
        discount_cap_pct=_bounded_int(raw, "discount_cap_pct", where, 0, 100),
        skus=skus,
        units=units,
        orders=orders,
        source=where,
    )


def nameplate_match(unit: Unit, sku: Sku, recoverable: tuple[str, ...]) -> bool:
    return (
        unit.failure in recoverable
        and unit.kva_mva == sku.kva_mva
        and unit.hv_kv == sku.hv_kv
        and unit.lv_kv == sku.lv_kv
        and sku.z_min_pct <= unit.z_pct <= sku.z_max_pct
    )


def standard_match(unit: Unit, sku: Sku, recoverable: tuple[str, ...]) -> bool:
    if unit.failure not in recoverable:
        return False
    if unit.kva_mva != sku.kva_mva or not (sku.z_min_pct <= unit.z_pct <= sku.z_max_pct):
        return False
    if unit.hv_kv == sku.hv_kv and unit.lv_kv == sku.lv_kv:
        return True
    return unit.interface_ok


def build_weeks(sku: Sku, index: int) -> int:
    """index is 1-based. The first build is half the new-unit lead time."""
    raw = sku.first_build_weeks - (index - 1) * sku.learn_weeks
    return max(sku.floor_weeks, raw)


def bespoke_offers(yard: Yard, sku: Sku) -> list[Offer]:
    offers = []
    for unit in yard.units:
        if unit.on_shelf:
            continue
        if nameplate_match(unit, sku, yard.recoverable):
            offers.append(Offer(unit.id, sku.id, unit.shop_weeks, "bespoke"))
    return offers


def standard_offers(yard: Yard, sku: Sku) -> list[Offer]:
    shelf = []
    builds = []
    for unit in yard.units:
        if not standard_match(unit, sku, yard.recoverable):
            continue
        if unit.on_shelf:
            shelf.append(unit)
        else:
            builds.append(unit)
    builds.sort(key=lambda unit: unit.id)
    offers = [Offer(unit.id, sku.id, sku.shelf_weeks, "shelf") for unit in shelf]
    for index, unit in enumerate(builds, start=1):
        offers.append(Offer(unit.id, sku.id, build_weeks(sku, index), "build"))
    return offers


def new_offers(yard: Yard, sku: Sku) -> list[Offer]:
    return [
        Offer(f"new-{order.id}", sku.id, sku.new_weeks, "new")
        for order in yard.orders
        if order.sku == sku.id
    ]


def assign(orders: tuple[Order, ...], offers: list[Offer]) -> tuple[tuple[Assignment, ...], tuple[Miss, ...]]:
    used: set[str] = set()
    shipped: list[Assignment] = []
    missed: list[Miss] = []
    for order in sorted(orders, key=lambda item: (item.need_week, item.id)):
        choices = [
            offer
            for offer in offers
            if offer.sku_id == order.sku and offer.unit_id not in used and offer.weeks <= order.need_week
        ]
        if not choices:
            remaining = [offer.weeks for offer in offers if offer.sku_id == order.sku and offer.unit_id not in used]
            shipped_miss = None if not remaining else min(remaining)
            missed.append(Miss(order.id, order.sku, order.need_week, shipped_miss))
            continue
        pick = min(choices, key=lambda offer: (offer.weeks, offer.unit_id))
        used.add(pick.unit_id)
        shipped.append(
            Assignment(order.id, order.sku, order.need_week, pick.unit_id, pick.weeks, pick.kind)
        )
    return tuple(shipped), tuple(missed)


def price_failures(yard: Yard) -> tuple[Quote, ...]:
    counts: dict[str, int] = {}
    for unit in yard.units:
        counts[unit.failure] = counts.get(unit.failure, 0) + 1
    quotes: list[Quote] = []
    for failure in sorted(counts):
        seen = counts[failure]
        guild = yard.guild_usd[failure]
        quotable = failure in yard.recoverable
        learned = None
        if quotable:
            discount = min(yard.discount_cap_pct, (seen - 1) * yard.learn_discount_pct)
            learned = guild * (100 - discount) // 100
        quotes.append(Quote(failure, seen, guild, learned, quotable))
    return tuple(quotes)


def run_yard(yard: Yard) -> Report:
    bespoke: list[Offer] = []
    standard: list[Offer] = []
    fresh: list[Offer] = []
    for sku in yard.skus:
        bespoke.extend(bespoke_offers(yard, sku))
        standard.extend(standard_offers(yard, sku))
        fresh.extend(new_offers(yard, sku))
    policies = (
        PolicyResult("bespoke-reman", *assign(yard.orders, bespoke)),
        PolicyResult("standard-iron", *assign(yard.orders, standard)),
        PolicyResult("new-build", *assign(yard.orders, fresh)),
    )
    return Report(yard, policies, price_failures(yard))


def format_result_line(report: Report) -> str:
    yard = report.yard
    total = len(yard.orders)
    bespoke = report.policy("bespoke-reman")
    standard = report.policy("standard-iron")
    fresh = report.policy("new-build")
    storm = min((order for order in yard.orders if order.sku == yard.skus[0].id), key=lambda order: (order.need_week, order.id))
    storm_ship = next(item for item in standard.shipped if item.order_id == storm.id)
    sku = yard.sku(storm.sku)
    fastest = min(offer.weeks for offer in bespoke_offers(yard, sku))
    return (
        f"standard-iron ships {standard.shipped_count}/{total} orders "
        f"(bespoke-reman {bespoke.shipped_count}/{total}, new-build {fresh.shipped_count}/{total}). "
        f"The {storm.need_week}-week {storm.sku} order ships from the shelf in {storm_ship.weeks} weeks; "
        f"bespoke's fastest exact nameplate is {fastest} weeks."
    )


def format_table(report: Report) -> str:
    yard = report.yard
    new_weeks = yard.skus[0].new_weeks
    first = yard.skus[0].first_build_weeks
    lines = [
        f"program {yard.program}  orders {len(yard.orders)}  new {new_weeks} weeks  first standard build {first} weeks",
        "",
        f"{'policy':<14}  {'shipped':>7}  {'missed':>6}  miss_ids",
        f"{'-' * 14}  {'-' * 7}  {'-' * 6}  {'-' * 24}",
    ]
    for policy in report.policies:
        misses = ", ".join(miss.order_id for miss in policy.missed) or "-"
        lines.append(
            f"{policy.name:<14}  {policy.shipped_count:>7}  {policy.missed_count:>6}  {misses}"
        )
    lines.extend(
        [
            "",
            f"{'failure':<16}  {'seen':>4}  {'guild_usd':>10}  {'learned_usd':>11}  quote",
            f"{'-' * 16}  {'-' * 4}  {'-' * 10}  {'-' * 11}  {'-' * 5}",
        ]
    )
    for quote in report.quotes:
        learned = "-" if quote.learned_usd is None else str(quote.learned_usd)
        flag = "yes" if quote.quotable else "no"
        lines.append(
            f"{quote.failure:<16}  {quote.seen:>4}  {quote.guild_usd:>10}  {learned:>11}  {flag}"
        )
    lines.append("")
    lines.append(report.result_line)
    return "\n".join(lines)


def format_details(report: Report) -> str:
    lines = ["assignments"]
    for policy in report.policies:
        for item in policy.shipped:
            lines.append(
                f"  {policy.name:<14}  {item.order_id}  {item.sku_id}  need {item.need_week:>3}  "
                f"{item.unit_id:<6}  {item.weeks:>3} weeks  {item.kind}"
            )
        for miss in policy.missed:
            soonest = "none" if miss.soonest_week is None else f"{miss.soonest_week} weeks"
            lines.append(
                f"  {policy.name:<14}  {miss.order_id}  {miss.sku_id}  need {miss.need_week:>3}  "
                f"miss  soonest {soonest}"
            )
    return "\n".join(lines)


def to_json(report: Report) -> str:
    def policy_row(policy: PolicyResult) -> dict[str, object]:
        return {
            "policy": policy.name,
            "shipped": policy.shipped_count,
            "missed": policy.missed_count,
            "miss_ids": [miss.order_id for miss in policy.missed],
            "assignments": [
                {
                    "order": item.order_id,
                    "sku": item.sku_id,
                    "need_week": item.need_week,
                    "unit": item.unit_id,
                    "weeks": item.weeks,
                    "kind": item.kind,
                }
                for item in policy.shipped
            ],
        }

    payload = {
        "program": report.yard.program,
        "policies": [policy_row(policy) for policy in report.policies],
        "quotes": [
            {
                "failure": quote.failure,
                "seen": quote.seen,
                "guild_usd": quote.guild_usd,
                "learned_usd": quote.learned_usd,
                "quotable": quote.quotable,
            }
            for quote in report.quotes
        ],
        "result": report.result_line,
    }
    return json.dumps(payload, indent=2) + "\n"


def _load_guild(raw: object, recoverable: tuple[str, ...], where: str) -> dict[str, int]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{where}: guild_usd must be an object")
    parsed: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{where}: guild failure names must be strings")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{where}: guild_usd {key} must be a non-negative integer")
        parsed[key] = value
    for name in recoverable:
        if name not in parsed:
            raise ValueError(f"{where}: guild_usd missing {name}")
    return parsed


def _load_sku(raw: object, where: str) -> Sku:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: expected an object")
    sku_id = _need_str(raw, "id", where)
    sku = Sku(
        id=sku_id,
        kva_mva=_bounded_int(raw, "kva_mva", f"{where} {sku_id}", 1, 1000),
        hv_kv=_need_str(raw, "hv_kv", f"{where} {sku_id}"),
        lv_kv=_need_str(raw, "lv_kv", f"{where} {sku_id}"),
        z_min_pct=_bounded_int(raw, "z_min_pct", f"{where} {sku_id}", 0, 100),
        z_max_pct=_bounded_int(raw, "z_max_pct", f"{where} {sku_id}", 0, 100),
        new_weeks=_bounded_int(raw, "new_weeks", f"{where} {sku_id}", 1, 1000),
        shelf_weeks=_bounded_int(raw, "shelf_weeks", f"{where} {sku_id}", 1, 1000),
        first_build_weeks=_bounded_int(raw, "first_build_weeks", f"{where} {sku_id}", 1, 1000),
        learn_weeks=_bounded_int(raw, "learn_weeks", f"{where} {sku_id}", 0, 1000),
        floor_weeks=_bounded_int(raw, "floor_weeks", f"{where} {sku_id}", 1, 1000),
    )
    if sku.z_min_pct > sku.z_max_pct:
        raise ValueError(f"{where} {sku_id}: impedance window is inverted")
    if sku.first_build_weeks * 2 != sku.new_weeks:
        raise ValueError(f"{where} {sku_id}: first build must be half the new-unit lead time")
    if sku.floor_weeks > sku.first_build_weeks:
        raise ValueError(f"{where} {sku_id}: floor is above the first build")
    return sku


def _load_unit(raw: object, where: str, guild: dict[str, int]) -> Unit:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: expected an object")
    unit_id = _need_str(raw, "id", where)
    failure = _need_str(raw, "failure", f"{where} {unit_id}")
    if failure not in guild:
        raise ValueError(f"{where} {unit_id}: failure {failure} has no guild price")
    return Unit(
        id=unit_id,
        kva_mva=_bounded_int(raw, "kva_mva", f"{where} {unit_id}", 1, 1000),
        hv_kv=_need_str(raw, "hv_kv", f"{where} {unit_id}"),
        lv_kv=_need_str(raw, "lv_kv", f"{where} {unit_id}"),
        z_pct=_bounded_int(raw, "z_pct", f"{where} {unit_id}", 0, 100),
        failure=failure,
        shop_weeks=_bounded_int(raw, "shop_weeks", f"{where} {unit_id}", 0, 1000),
        on_shelf=_need_bool(raw, "on_shelf", f"{where} {unit_id}"),
        interface_ok=_need_bool(raw, "interface_ok", f"{where} {unit_id}"),
    )


def _load_order(raw: object, where: str) -> Order:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: expected an object")
    order_id = _need_str(raw, "id", where)
    return Order(
        id=order_id,
        sku=_need_str(raw, "sku", f"{where} {order_id}"),
        need_week=_bounded_int(raw, "need_week", f"{where} {order_id}", 1, 1000),
    )


def _need_list(raw: dict, key: str, where: str) -> list:
    value = raw.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{where}: {key} must be a list")
    return value


def _need_str_list(raw: dict, key: str, where: str) -> list[str]:
    value = _need_list(raw, key, where)
    names = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise ValueError(f"{where}: {key}[{index}] must be a non-empty string")
        names.append(item)
    return names


def _need_str(raw: dict, key: str, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: {key} must be a non-empty string")
    return value


def _need_bool(raw: dict, key: str, where: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{where}: {key} must be true or false")
    return value


def _bounded_int(raw: dict, key: str, where: str, low: int, high: int) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where}: {key} must be an integer")
    if value < low or value > high:
        raise ValueError(f"{where}: {key} must be from {low} to {high}")
    return value


def _unique(values: list[str], message: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(message)
