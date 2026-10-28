def tax_on(cents: int) -> int:
    return cents // 10


def with_tax(cents: int) -> int:
    return cents + tax_on(cents)
