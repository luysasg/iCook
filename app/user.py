"""getUser → a compact profile for personalization (first name, saved payments, behavior).

Only an allow-list leaves the response: email, phone, full name, tax ID and any user ID
never reach the session, the LLM or the UI. Tiers (from the docs):
BASIC fullName/email/phone · ADVANCED + paymentMethods · BEHAVIOR + profileData.
Behavior shapes are undocumented beyond `userBehaviorDescription`, so they are read
leniently; counts may come as strings and are converted before any math.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .ifood import Unauthorized
from .jsonutil import as_number
from .shopping import PaymentOption

PERSONA_MAX_CHARS = 600
WEEKDAYS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
WEEKDAY_WORDS = {
    "MONDAY": "segunda", "TUESDAY": "terça", "WEDNESDAY": "quarta", "THURSDAY": "quinta", "FRIDAY": "sexta",
    "SATURDAY": "sábado", "SUNDAY": "domingo", "WEEKDAY": "dias úteis", "WEEKEND": "fim de semana",
}
SHIFT_WORDS = {
    "BREAKFAST": "manhã", "MORNING": "manhã", "LUNCH": "almoço", "AFTERNOON": "tarde", "SNACK": "tarde",
    "DINNER": "jantar", "EVENING": "jantar", "NIGHT": "jantar", "DAWN": "madrugada", "LATE_NIGHT": "madrugada",
}
MERCHANT_TYPES = {"GROCERIES": "Mercado", "RESTAURANT": "Restaurante", "PHARMACY": "Farmácia", "PET": "Pet shop",
                  "DRINKS": "Bebidas", "BEVERAGES": "Bebidas", "CONVENIENCE": "Conveniência"}
CATEGORY_KEYS = ("topMerchantTypes", "favoriteCategories", "topCategories", "favoriteMerchantTypes")
PATTERN_KEYS = ("topWeekDayShift", "topWeekdayShift", "topWeekDayShifts")
COUNT_TABLE_KEYS = ("ordersByMerchantTypeWeekdayAndShift", "ordersByWeekdayAndShift", "ordersByDay")
CHIPS_BY_SHIFT = {"almoço": "Algo leve pro almoço", "jantar": "Algo prático pro jantar", "manhã": "Café da manhã"}


class ProfileError(Exception):
    """getUser answered with an inline error (or nothing usable)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class SavedPayment:
    kind: str  # e.g. credit_card, pix
    last_digits: str | None
    method_id: str | None  # never shown or sent to the LLM; only matched against listPaymentMethods

    def label(self) -> str:
        kind = self.kind.replace("_", " ").strip().capitalize() or "Cartão"
        return f"{kind} final {self.last_digits}" if self.last_digits else kind


@dataclass
class UserProfile:
    first_name: str | None = None
    payments: list[SavedPayment] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)  # most ordered first, e.g. "Mercado (12 pedidos)"
    patterns: list[str] = field(default_factory=list)  # e.g. "dias úteis · almoço"
    usual_shifts: list[str] = field(default_factory=list)  # normalized: almoço, jantar...
    persona: str | None = None

    def prompt_block(self) -> str:
        """Only what helps the conversation; empty when getUser had nothing useful."""
        lines = []
        if self.first_name:
            lines.append(f"Primeiro nome: {self.first_name}")
        if self.payments:
            lines.append("Pagamentos salvos: " + "; ".join(p.label() for p in self.payments))
        if self.categories:
            lines.append("Categorias que mais pede: " + ", ".join(self.categories))
        if self.patterns:
            lines.append("Dias/horários em que costuma pedir: " + "; ".join(self.patterns))
        if self.persona:
            lines.append(f"Resumo de hábitos (iFood): {self.persona}")
        if not lines:
            return ""
        return "\n## Perfil da pessoa (do iFood; só o que está aqui, nunca invente preferências)\n" + "\n".join(lines)


def parse_user(result: Any) -> UserProfile:
    """getUser response → profile. Raises Unauthorized / ProfileError on an inline error."""
    if not isinstance(result, dict):
        raise ProfileError("EMPTY_RESPONSE")
    error = result.get("error")
    if isinstance(error, dict) or result.get("success") is False:
        code = str((error or {}).get("code") or "UNKNOWN") if isinstance(error, dict) else "UNKNOWN"
        if code == "AUTHENTICATION_REQUIRED":
            raise Unauthorized
        raise ProfileError(code)
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    behavior = data.get("profileData") if isinstance(data.get("profileData"), dict) else {}
    profile = UserProfile(first_name=first_name(data.get("fullName") or data.get("name")))
    profile.payments = _payments(data.get("paymentMethods"))
    profile.categories = [
        f"{_merchant_type(label)} ({count:g} pedidos)" if count is not None else _merchant_type(label)
        for label, count in _counts(_pick(behavior, data, CATEGORY_KEYS))[:3]
    ]
    for label, _ in _counts(_pick(behavior, data, PATTERN_KEYS))[:2]:
        _add_pattern(profile, label)
    for path, count in _leaf_counts(_pick(behavior, data, COUNT_TABLE_KEYS))[:3]:
        _add_pattern(profile, " ".join(path), count)
    persona = behavior.get("userBehaviorDescription")
    if isinstance(persona, str) and persona.strip():
        text = re.sub(r"\s+", " ", persona).strip()
        profile.persona = text[:PERSONA_MAX_CHARS] + ("…" if len(text) > PERSONA_MAX_CHARS else "")
    return profile


def first_name(full_name: Any) -> str | None:
    if not isinstance(full_name, str):
        return None
    word = next((w for w in full_name.split() if re.search(r"[^\W\d_]", w)), None)
    if not word:
        return None
    word = re.sub(r"[^\w'-]", "", word)
    return word.capitalize() if word.isupper() or word.islower() else word


def preferred_payment(profile: UserProfile | None, options: list[PaymentOption]) -> int | None:
    """Index of the option matching a payment saved on the account (the user still confirms it)."""
    if not profile:
        return None
    for saved in profile.payments:
        for n, option in enumerate(options):
            if saved.method_id and saved.method_id in (option.method_id, option.card_token):
                return n
        for n, option in enumerate(options):
            if saved.last_digits and option.card_token and f"final {saved.last_digits}" in option.label:
                return n
        if not saved.last_digits:
            word = "pix" if "pix" in saved.kind.lower() else "dinheiro" if "cash" in saved.kind.lower() else None
            for n, option in enumerate(options):
                if word and word in option.label.lower():
                    return n
    return None


def now_sp() -> datetime:
    try:
        return datetime.now(ZoneInfo("America/Sao_Paulo"))
    except ZoneInfoNotFoundError:
        return datetime.now()


def shift_of(moment: datetime) -> str:
    hour = moment.hour
    if 5 <= hour < 11:
        return "manhã"
    if 11 <= hour < 15:
        return "almoço"
    if 15 <= hour < 18:
        return "tarde"
    if 18 <= hour < 23:
        return "jantar"
    return "madrugada"


def now_line(moment: datetime) -> str:
    return f"Agora: {WEEKDAYS[moment.weekday()]}, {moment:%H:%M} ({shift_of(moment)})"


def craving_chips(profile: UserProfile | None, base: list[str], moment: datetime) -> list[str]:
    """When the user usually orders at this time of day, lead with a matching suggestion."""
    shift = shift_of(moment)
    chip = CHIPS_BY_SHIFT.get(shift)
    if not profile or not chip or shift not in profile.usual_shifts:
        return base
    return [chip] + [c for c in base if c != chip][:3]


# ---------------------------------------------------------------- lenient readers


def _pick(primary: dict[str, Any], fallback: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for source in (primary, fallback):
        for key in keys:
            if source.get(key) not in (None, "", [], {}):
                return source[key]
    return None


def _payments(raw: Any) -> list[SavedPayment]:
    payments = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        kind = next((str(entry[k]) for k in ("type", "method", "name", "brand") if entry.get(k)), "")
        digits = entry.get("lastDigits") or entry.get("last4") or entry.get("lastFourDigits")
        method_id = entry.get("id") or entry.get("paymentMethodId") or entry.get("cardTokenId")
        payments.append(SavedPayment(kind, str(digits) if digits else None, str(method_id) if method_id else None))
    return payments


def _counts(raw: Any) -> list[tuple[str, float | None]]:
    """{"GROCERIES": "12"} / [{"merchantType": "GROCERIES", "count": "12"}] / ["GROCERIES"] / "GROCERIES"
    → [(label, count)], most frequent first (lists without counts keep their order)."""
    items: list[tuple[str, float | None]] = []
    if isinstance(raw, str) and raw.strip():
        items.append((raw.strip(), None))
    elif isinstance(raw, dict):
        counted = [(str(k), as_number(v)) for k, v in raw.items() if not isinstance(v, dict | list)]
        if any(count is not None for _, count in counted):
            items += [(label, count) for label, count in counted if count is not None]
        else:  # a single object, e.g. {"weekDay": "MONDAY", "shift": "LUNCH"}
            items.append((" ".join(str(v) for v in raw.values() if isinstance(v, str)), None))
    elif isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, str):
                items.append((entry, None))
            elif isinstance(entry, dict):
                label = " ".join(str(v) for k, v in entry.items() if isinstance(v, str) and not _is_count_key(k))
                count = next((as_number(entry[k]) for k in entry if _is_count_key(k)), None)
                if label.strip():
                    items.append((label.strip(), count))
    items = [(label, count) for label, count in items if label and count != 0]
    return sorted(items, key=lambda item: -(item[1] or 0)) if any(c is not None for _, c in items) else items


def _leaf_counts(raw: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], float]]:
    """Nested count tables ({"GROCERIES": {"MONDAY": {"LUNCH": "3"}}}) → [(path, count)], biggest first."""
    found = []
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, dict | list):
                found += _leaf_counts(value, path + (str(key),))
            elif (count := as_number(value)) and not _is_count_key(key):
                found.append((path + (str(key),), count))
    elif isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                labels = tuple(str(v) for k, v in entry.items() if isinstance(v, str) and not _is_count_key(k))
                count = next((as_number(entry[k]) for k in entry if _is_count_key(k)), None)
                if labels and count:
                    found.append((path + labels, count))
    return sorted(found, key=lambda item: -item[1])


def _is_count_key(key: str) -> bool:
    return key.lower() in ("count", "orders", "total", "quantity", "value", "ordercount", "orderscount")


def _merchant_type(label: str) -> str:
    return MERCHANT_TYPES.get(label.strip().upper(), label.strip().replace("_", " ").capitalize())


def _add_pattern(profile: UserProfile, label: str, count: float | None = None) -> None:
    words = [w for w in re.split(r"[\s_/,-]+", label.upper()) if w]
    days = [WEEKDAY_WORDS[w] for w in words if w in WEEKDAY_WORDS]
    shifts = [SHIFT_WORDS[w] for w in words if w in SHIFT_WORDS]
    types = [MERCHANT_TYPES[w] for w in words if w in MERCHANT_TYPES]
    if not days and not shifts:
        return
    text = " · ".join(types + days + shifts) + (f" ({count:g} pedidos)" if count is not None else "")
    if text not in profile.patterns:
        profile.patterns.append(text)
    profile.usual_shifts += [s for s in shifts if s not in profile.usual_shifts]

