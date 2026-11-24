class CodeName(str):
    """错误码字符串；可同时等于少量历史/近义错误码，便于不同接口约定。"""

    __slots__ = ('aliases',)

    def __new__(cls, primary, aliases=()):
        obj = str.__new__(cls, primary)
        obj.aliases = tuple(aliases)
        return obj

    def __eq__(self, other):
        if str.__eq__(self, other):
            return True
        return other in self.aliases

    def __hash__(self):
        return str.__hash__(self)


class RoastError(Exception):
    def __init__(self, code, message, status=422, payload=None, aliases=()):
        super().__init__(message)
        self.code = CodeName(code, aliases) if aliases else code
        self.status = status
        self.payload = payload
