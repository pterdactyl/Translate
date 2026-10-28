def sum_to(n: int) -> int:
    summed = 0
    i = 0
    for i in range(1, n + 1):
        summed = summed + i
    return summed


def countdown(n: int) -> int:
    summed = 0
    i = 0
    for i in range(n, 0, -1):
        summed = summed + i
    return summed


def while_sum(n: int) -> int:
    summed = 0
    i = 0
    while i < n:
        i += 1
        summed += i
    return summed


def evens(n: int) -> int:
    summed = 0
    i = 0
    for i in range(n):
        if i % 2 == 1:
            continue
        summed = summed + i
    return summed


def until(n: int, stop: int) -> int:
    summed = 0
    i = 0
    for i in range(n):
        if i == stop:
            break
        summed = summed + i
    return summed


def last_assigned(n: int) -> int:
    i = -1
    for i in range(n):
        pass
    return i


def overwritten(n: int) -> int:
    i = -1
    for i in range(n):
        i = i + 10
    return i


def empty_sum() -> int:
    summed = 0
    i = 0
    for i in range(0):
        summed = summed + 1
    return summed


def stopped(n: int, stop: int) -> int:
    i = 0
    while i < n:
        if i == stop:
            break
        i = i + 1
    return i
