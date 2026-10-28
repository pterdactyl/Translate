from .tax import tax_on, with_tax


def line_total(price: int, count: int) -> int:
    return price * count


def charged(price: int, count: int) -> int:
    return with_tax(line_total(price, count))


def tax_only(price: int, count: int) -> int:
    return tax_on(line_total(price, count))
