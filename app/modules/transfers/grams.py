"""克重与产地组成的精确运算工具。

全模块沿用卷联约定：内部使用 :class:`fractions.Fraction` 精算，
对外输出分数字符串。:class:`Gram` 是字符串子类，同时能与 int/Fraction
按数值比较，方便不同风格的断言。
"""
from fractions import Fraction


class Gram(str):
    """序列化为字符串、比较时可按分数或数字判等的克重值。"""

    __slots__ = ()

    def __new__(cls, value=None):
        if value is None or value == '':
            amount = Fraction(0)
        elif isinstance(value, Fraction):
            amount = value
        elif isinstance(value, int):
            amount = Fraction(value)
        elif isinstance(value, float):
            amount = Fraction(str(value))
        else:
            text = str(value).strip()
            amount = Fraction(text)
        return str.__new__(cls, str(amount))

    def __eq__(self, other):
        if isinstance(other, str):
            try:
                return Fraction(self) == Fraction(other)
            except (ValueError, ZeroDivisionError):
                return str.__eq__(self, other)
        if isinstance(other, (int, Fraction)):
            return Fraction(self) == Fraction(other)
        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self):
        return str.__hash__(self)


def parse_gram(value, field='quantity_g'):
    """把 JSON / 表单 / 字符串中的重量解析为 Fraction。"""
    if value is None or value == '':
        return None
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):
        return Fraction(int(value))
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(str(value))
    text = str(value).strip()
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError) as exc:
        from app.errors import RoastError
        raise RoastError('BAD_QUANTITY', f'{field} 不是合法克重：{value!r}') from exc


def require_positive(value, field='quantity_g'):
    amount = parse_gram(value, field)
    if amount is None:
        from app.errors import RoastError
        raise RoastError('BAD_QUANTITY', f'{field} 缺少克重')
    if amount <= 0:
        from app.errors import RoastError
        raise RoastError('BAD_QUANTITY', f'{field} 必须大于零')
    return amount


def as_origin_map(value):
    """接受 {产地: 克重} 映射，返回 Fraction 映射。"""
    if not value:
        return {}
    if isinstance(value, str):
        # 允许 'ET-01:180,BR-08:120' 这类表单写法
        result = {}
        for chunk in value.replace(';', ',').split(','):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ':' in chunk:
                key, gram = chunk.split(':', 1)
            elif '=' in chunk:
                key, gram = chunk.split('=', 1)
            else:
                continue
            result[key.strip()] = parse_gram(gram.strip(), 'origin_g')
        return result
    result = {}
    for key, gram in dict(value).items():
        amount = parse_gram(gram, f'origin_g.{key}')
        if amount is not None:
            result[str(key)] = amount
    return result


def total_of(origin_g):
    return sum(origin_g.values(), Fraction())


def scale_map(origin_g, numerator, denominator=None):
    """按 numerator/denominator 线性缩放产地组成。

    denominator 为 None 时，numerator 直接是比例 Fraction。
    """
    if denominator is None:
        ratio = numerator
    else:
        if denominator == 0:
            return {key: Fraction() for key in origin_g}
        ratio = Fraction(numerator, denominator)
    return {key: value * ratio for key, value in origin_g.items()}


def share_of(origin_g, part_g, whole_g):
    """整卷 whole_g 中取走 part_g 时，按均质比例携带的产地克重。"""
    if whole_g is None or whole_g == 0:
        return {key: Fraction() for key in origin_g}
    return scale_map(origin_g, Fraction(part_g, whole_g))


def add_maps(*maps):
    result = {}
    for mapping in maps:
        for key, value in mapping.items():
            result[key] = result.get(key, Fraction()) + value
    return result


def sub_maps(left, right):
    result = dict(left)
    for key, value in right.items():
        result[key] = result.get(key, Fraction()) - value
    return result


def view_map(origin_g):
    """序列化为 Gram 字符串映射，键排序保证稳定。"""
    return {key: Gram(value) for key, value in sorted(origin_g.items())}


def plain_map(origin_g):
    return {key: str(value) for key, value in origin_g.items()}


def is_zero_map(origin_g):
    return all(value == 0 for value in origin_g.values())
