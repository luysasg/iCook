"""The recipe the LLM proposes, and the user answers parsed in code (pantry, document)."""

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .shopping import Ingredient

log = logging.getLogger("recipe")


@dataclass
class Recipe:
    title: str
    description: str
    servings: int | None
    ingredients: list[Ingredient]
    steps: list[str]
    have: list[int] = field(default_factory=list)  # ingredient numbers the user already has

    def view(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "servings": self.servings,
            "ingredients": [
                {"number": n, "name": i.name, "amount": f"{i.quantity:g} {i.unit}"}
                for n, i in enumerate(self.ingredients, 1)
            ],
            "steps": self.steps,
            "have": self.have,
        }

    def as_text(self) -> str:
        lines = [f"Receita: {self.title} — {self.description}", "Ingredientes:"]
        lines += [f"{n}. {i.label()}" for n, i in enumerate(self.ingredients, 1)]
        lines += ["Modo de preparo:"] + [f"{n}. {step}" for n, step in enumerate(self.steps, 1)]
        return "\n".join(lines)


def parse_ingredients(raw: Any) -> list[Ingredient]:
    items = []
    for entry in raw if isinstance(raw, list) else []:
        try:
            items.append(
                Ingredient(
                    name=str(entry["name"]).strip(),
                    search_term=str(entry.get("search_term") or entry["name"]).strip(),
                    quantity=float(entry.get("quantity") or 1),
                    unit=str(entry.get("unit") or "unidade").strip(),
                )
            )
        except (KeyError, TypeError, ValueError):
            log.warning("ignoring malformed list entry: %r", entry)
    return items


def parse_recipe(raw: Any) -> Recipe | None:
    if not isinstance(raw, dict):
        return None
    ingredients = parse_ingredients(raw.get("ingredients"))
    if not ingredients:
        return None
    servings = raw.get("servings")
    return Recipe(
        title=str(raw.get("title") or "Receita").strip(),
        description=str(raw.get("description") or "").strip(),
        servings=servings if isinstance(servings, int) and servings > 0 else None,
        ingredients=ingredients,
        steps=[str(step).strip() for step in raw.get("steps") or [] if str(step).strip()],
    )


def parse_pantry_answer(text: str, count: int) -> list[int] | None:
    """Ingredient numbers from "1, 3 e 5" / "nenhum" / "todos"; None when it needs the LLM (names)."""
    normalized = text.strip().lower().rstrip(".!")
    if re.fullmatch(r"(n[aã]o tenho )?(nenhum|nenhuma|nada)( deles| delas)?", normalized):
        return []
    if re.fullmatch(r"(tenho )?(todos|todas|tudo)( eles| elas)?", normalized):
        return list(range(1, count + 1))
    numbers = re.sub(r"^(j[aá] )?tenho:?\s*", "", normalized)
    if re.fullmatch(r"[\d\s,;e.]+", numbers) and re.search(r"\d", numbers):
        return sorted({n for n in map(int, re.findall(r"\d+", numbers)) if 1 <= n <= count})
    return None


def valid_document(text: str) -> str | None:
    """CPF (11 digits), CNPJ (14 digits) or a passport number (foreign accounts)."""
    digits = re.sub(r"\D", "", text)
    if len(digits) in (11, 14) and not re.search(r"[A-Za-z]", text):
        return digits
    passport = re.sub(r"[\s.-]", "", text).upper()
    if re.fullmatch(r"(?=.*[A-Z])(?=.*\d)[A-Z0-9]{6,9}", passport):
        return passport
    return None
