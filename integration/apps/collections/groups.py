def first(items: list[int]) -> int:
    return items[0]


def at(items: list[int], index: int) -> int:
    return items[index]


def pair(left: int, right: int) -> list[int]:
    built: list[int] = [left, right]
    return built


def head(left: int, right: int) -> int:
    built: list[int] = pair(left, right)
    return built[0]


def replace_first(items: list[int], value: int) -> int:
    items[0] = value
    return items[0] + items[1]


def word(names: list[str], index: int) -> str:
    return names[index]


def average_ends(values: list[float]) -> float:
    return (values[0] + values[1]) / 2.0


def both_flags(flags: list[bool]) -> bool:
    return flags[0] and flags[1]


def price(menu: dict[str, int], item: str) -> int:
    return menu[item]


def pen_price() -> int:
    menu: dict[str, int] = {"pen": 2, "book": 5}
    return menu["pen"]


def update_price(menu: dict[str, int], item: str, value: int) -> int:
    menu[item] = value
    return menu[item]


def at_or_zero(items: list[int], index: int) -> int:
    try:
        return items[index]
    except IndexError:
        return 0


def price_or_zero(menu: dict[str, int], item: str) -> int:
    try:
        return menu[item]
    except KeyError:
        return 0
