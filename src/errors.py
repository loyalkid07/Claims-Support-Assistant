"""Controlled policy errors without caller-provided values in messages."""


class PolicyError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
