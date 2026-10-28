def floor(left: int, right: int) -> int:
    return left // right


def true_div(left: int, right: int) -> float:
    return left / right


def require_positive(value: int) -> int:
    assert value > 0, "positive"
    return value


def safe_floor(left: int, right: int) -> int:
    try:
        return left // right
    except ZeroDivisionError:
        return 0


def safe_div(left: int, right: int) -> float:
    try:
        return left / right
    except ZeroDivisionError:
        return 0.0


def safe_positive(value: int) -> int:
    try:
        assert value > 0, "positive"
        return value
    except AssertionError:
        return 0


def safe_step(step: int) -> int:
    summed = 0
    i = 0
    try:
        for i in range(0, 1, step):
            summed = summed + 1
    except ValueError:
        return -1
    return summed


def classify(mode: int) -> int:
    try:
        if mode == 0:
            raise ValueError("bad")
        if mode == 1:
            return 1 // 0
        return mode
    except ValueError:
        return -1
    except ZeroDivisionError:
        return -2


def reject(flag: bool) -> str:
    if flag:
        raise ValueError("bad")
    return "ok"
