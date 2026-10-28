def add(left: int, right: int) -> int:
    return left + right


def sub(left: int, right: int) -> int:
    return left - right


def mul(left: int, right: int) -> int:
    return left * right


def div(left: int, right: int) -> float:
    return left / right


def floordiv(left: int, right: int) -> int:
    return left // right


def mod(left: int, right: int) -> int:
    return left % right


def negate(value: int) -> int:
    return -value


def absolute(value: int) -> int:
    return abs(value)


def lowest(left: int, right: int) -> int:
    return min(left, right)


def highest(left: float, right: float) -> float:
    return max(left, right)


def clamp(value: int, low: int, high: int) -> int:
    if value < low:
        return low
    if value > high:
        return high
    return value


def sign(value: int) -> int:
    if value < 0:
        return -1
    if value == 0:
        return 0
    return 1


def mean(left: float, right: float) -> float:
    return (left + right) / 2.0


def choose(flag: bool, left: int, right: int) -> int:
    if flag:
        return left
    return right


def either_value(flag: bool, left: int, right: int) -> int:
    return left if flag else right


def in_range(value: int, low: int, high: int) -> bool:
    return low <= value <= high


def both(left: bool, right: bool) -> bool:
    return left and right


def either(left: bool, right: bool) -> bool:
    return left or right


def guarded(flag: bool) -> bool:
    return flag and (1 / 0 > 0)


def label(name: str) -> str:
    return "item:" + name


def same(left: str, right: str) -> bool:
    return left == right


def discount(price: int, amount: int) -> int:
    reduced: int = sub(price, amount)
    if reduced < 0:
        return 0
    return reduced


def line(price: int, count: int) -> int:
    return mul(price, count)


def payable(price: int, count: int, amount: int) -> int:
    return discount(line(price, count), amount)
