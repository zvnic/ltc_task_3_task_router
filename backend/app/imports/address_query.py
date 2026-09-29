"""Разбор адреса заявки в запрос к геокодеру.

В выгрузках заявок адрес записан сокращённо и по-разному: «Город Москва,
пр-кт.Волгоградский, д. 97 к 1», «МО, г. Кашира Кржижановского ул. д. 5/1»,
«г.Город Москва, б-р.Самаркандский, д. 10к2». OpenStreetMap хранит улицу полным
названием («Волгоградский проспект»), а дом — в московской записи «97 к1», «24/30 с1».
Модуль приводит адрес к такому виду и выдаёт варианты запроса от точного к грубому.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Сокращение типа улицы → полное слово в названиях OpenStreetMap.
STREET_TYPES: dict[str, str] = {
    "ул": "улица",
    "пр-кт": "проспект",
    "просп": "проспект",
    "б-р": "бульвар",
    "бульв": "бульвар",
    "проезд": "проезд",
    "пр-зд": "проезд",
    "пр-д": "проезд",
    "пер": "переулок",
    "наб": "набережная",
    "ш": "шоссе",
    "пл": "площадь",
    "туп": "тупик",
}
# Города Московской области в зонах; всё остальное — Москва.
OBLAST_CITIES = ("Домодедово", "Кашира", "Ступино")

_TYPE_PATTERN = "|".join(
    re.escape(key) for key in sorted(STREET_TYPES, key=len, reverse=True)
)
# «ул.Маршала Чуйкова», «ул Юных Ленинцев», «проезд Орехово-Зуевский»
_PREFIX_STREET = re.compile(
    rf"(?:^|[\s,])(?P<type>{_TYPE_PATTERN})\.?\s*(?P<name>[^,]+?)\s*(?=,|\sд\.?\s|$)",
    re.IGNORECASE,
)
# «Кржижановского ул. д. 5/1», «Булатниковский пр-зд. д. 6к1»
_SUFFIX_STREET = re.compile(
    rf"(?P<name>[А-ЯЁа-яё0-9-]+(?:\s[А-ЯЁа-яё0-9-]+)*?)\s+(?P<type>{_TYPE_PATTERN})\.?\s+д\.?\s",
    re.IGNORECASE,
)
_HOUSE = re.compile(r"(?:^|[\s,])д\.?\s*(?P<house>[^,]+?)\s*$", re.IGNORECASE)
# «97 к 1», «128к1», «24/30 стр. 1», «9Б стр. 1», «83с 4»
_HOUSE_PARTS = re.compile(
    r"^(?P<number>\d+[А-ЯЁа-яё]?(?:/\d+[А-ЯЁа-яё]?)?)\s*"
    r"(?:(?P<kind>к|корп|стр|с)\.?\s*(?P<part>\d+[А-ЯЁа-яё]?))?$",
    re.IGNORECASE,
)
_ORDINAL = re.compile(r"^\d+-[а-я]{1,2}$")
_FEMININE = frozenset({"улица", "набережная", "площадь"})
_NEUTER = frozenset({"шоссе"})
_FEMININE_ENDINGS = ("ая", "яя")
_MASCULINE_ENDINGS = ("ий", "ый", "ой")
_NEUTER_ENDINGS = ("ое", "ее")
_QUARTER = re.compile(r"\s+квартал\s+([^\s,]+)", re.IGNORECASE)
# Номер дома без корпуса и строения: «83с4» → «83», «9бс1» → «9б», «24/30с1» → «24/30».
# Буква — литера дома, только если за ней не идёт цифра корпуса или строения.
_HOUSE_BASE = re.compile(r"^(\d+(?:[а-я](?!\d))?(?:/\d+(?:[а-я](?!\d))?)?)")


def house_key(value: str) -> str:
    """Номер дома для сравнения: «97 к1», «97 корп. 1» и «97к1» дают одно и то же."""
    text = value.casefold().replace("ё", "е")
    text = text.replace("корпус", "к").replace("корп", "к")
    text = text.replace("строение", "с").replace("стр", "с")
    return re.sub(r"[\s.]", "", text)


def house_base_key(value: str) -> str:
    """Номер дома без корпуса и строения для сравнения: «83 с4» → «83»."""
    match = _HOUSE_BASE.match(house_key(value))
    return match.group(1) if match else ""


@dataclass(frozen=True)
class AddressQuery:
    """Адрес в виде OpenStreetMap: улица полным названием, дом, населённый пункт."""

    city: str
    street: str | None
    house: str | None
    house_base: str | None
    settlement: str | None = None

    def variants(self) -> list[tuple[str, str]]:
        """Запросы от точного к грубому: (точность, текст запроса)."""
        place = ", ".join(part for part in (self.settlement, self.city) if part)
        result: list[tuple[str, str]] = []
        if self.street and self.house:
            result.append(("house", f"{self.street}, {self.house}, {place}"))
        if self.street and self.house_base and self.house_base != self.house:
            result.append(("house_base", f"{self.street}, {self.house_base}, {place}"))
        if self.street:
            result.append(("street", f"{self.street}, {place}"))
        return result


def _full_street(street_type: str, name: str) -> str:
    """«Волгоградский» + «пр-кт» → «Волгоградский проспект».

    Прилагательное, согласованное с типом улицы, стоит впереди («3-я Карачаровская
    улица», «Большой Рогожский переулок», «Семёновская набережная»), имя в родительном
    падеже — после типа («улица Маршала Чуйкова», «2-я улица Синичкина»).
    """
    kind = STREET_TYPES[street_type.lower().rstrip(".")]
    words = name.split()
    if len(words) > 1 and _ORDINAL.match(words[-1]):
        # «Советский 1-й» → «1-й Советский»
        words = [words[-1], *words[:-1]]
    ordinal = words[0] if _ORDINAL.match(words[0]) else None
    rest = words[1:] if ordinal else words
    if not rest:
        return f"{kind} {name}"
    endings = (
        _FEMININE_ENDINGS
        if kind in _FEMININE
        else _NEUTER_ENDINGS if kind in _NEUTER else _MASCULINE_ENDINGS
    )
    if rest[-1].lower().endswith(endings):
        return " ".join([*words, kind])
    if ordinal:
        return " ".join([ordinal, kind, *rest])
    return " ".join([kind, *rest])


def _house(raw: str) -> tuple[str | None, str | None]:
    value = " ".join(raw.replace("стр.", "стр ").split())
    match = _HOUSE_PARTS.match(value.replace(" ", "") if " к " not in value else value)
    if match is None:
        match = _HOUSE_PARTS.match(value)
    if match is None:
        return None, None
    number = match.group("number").upper()
    part = match.group("part")
    if not part:
        return number, number
    kind = match.group("kind").lower()
    letter = "к" if kind.startswith("к") else "с"
    return f"{number} {letter}{part.upper()}", number


def parse_address(address: str, district: str = "") -> AddressQuery:
    """Разобрать адрес выгрузки. Район из CSV подсказывает город области."""
    text = " ".join(address.replace("ё", "е").replace("Ё", "Е").split())
    city = next(
        (name for name in OBLAST_CITIES if name.lower() in f"{district} {text}".lower()),
        "Москва",
    )
    settlement_match = re.search(r"пгт\.?\s*([^,]+)", text, re.IGNORECASE)
    settlement = settlement_match.group(1).strip() if settlement_match else None
    if settlement:
        # «Востряково-1» в OpenStreetMap — микрорайон «Востряково».
        settlement = re.sub(r"-\d+$", "", settlement)

    house_raw = _HOUSE.search(text)
    house_text = house_raw.group("house") if house_raw else ""
    quarter = _QUARTER.search(text)
    if quarter is not None:
        # «б-р.Самаркандский Квартал 137а, д. к5» — дом записан номером квартала.
        text = text[: quarter.start()] + text[quarter.end():]
        house_text = f"{quarter.group(1)} {house_text}".strip()
    house, house_base = _house(house_text) if house_text else (None, None)

    street: str | None = None
    for match in _PREFIX_STREET.finditer(text):
        name = match.group("name").strip()
        if name and not name.lower().startswith("д."):
            street = _full_street(match.group("type"), name)
    if street is None:
        suffix = _SUFFIX_STREET.search(text)
        if suffix is not None:
            name = suffix.group("name").strip()
            # «Москва Бирюлевская ул.», «г. Кашира Кржижановского ул.» — город перед улицей.
            for prefix in ("Москва", "МО", "г.", "г", city):
                if name.startswith(prefix + " "):
                    name = name[len(prefix) + 1:].strip()
            street = _full_street(suffix.group("type"), name)
    return AddressQuery(
        city=city,
        street=street,
        house=house,
        house_base=house_base,
        settlement=settlement,
    )
