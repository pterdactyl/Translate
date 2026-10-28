class Rect:
    def area(self, width: int, height: int) -> int:
        return width * height

    def double(self, value: int) -> int:
        return value + value

    def perimeter(self, width: int, height: int) -> int:
        return self.double(width) + self.double(height)

    def larger_area(
        self,
        width: int,
        height: int,
        other_width: int,
        other_height: int,
    ) -> int:
        left: int = self.area(width, height)
        right: int = self.area(other_width, other_height)
        if left >= right:
            return left
        return right

    @staticmethod
    def sides() -> int:
        return 4


def corners() -> int:
    return 4
