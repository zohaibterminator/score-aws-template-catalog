"""Evaluate the small subset of HCL expressions used in conditions, e.g. `var.environment == "prod" ? 7 : 0`.

Anything outside the subset evaluates to UNKNOWN rather than guessing.
"""
from __future__ import annotations

import json
import re
from typing import Any


class _Unknown:
    def __repr__(self) -> str:
        return "UNKNOWN"


UNKNOWN: Any = _Unknown()

TOKEN = re.compile(r"""\s*(?:
    (?P<num>-?\d+(?:\.\d+)?)
  | (?P<str>"(?:[^"\\$]|\\.)*")
  | (?P<op>==|!=|<=|>=|&&|\|\||[<>?:!(),\[\]])
  | (?P<name>[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*)
)""", re.X)


def _tokens(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(text):
        match = TOKEN.match(text, pos)
        if not match or match.end() == pos:
            if text[pos:].strip() == "":
                break
            raise ValueError(f"unsupported syntax at: {text[pos:]!r}")
        kind = match.lastgroup
        out.append((kind, match.group(kind)))
        pos = match.end()
    return out


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]], env: dict[str, Any], locals_: dict[str, Any], depth: int):
        self.tokens, self.pos, self.env, self.locals, self.depth = tokens, 0, env, locals_, depth

    def peek(self) -> str | None:
        return self.tokens[self.pos][1] if self.pos < len(self.tokens) else None

    def take(self, expected: str | None = None) -> tuple[str, str]:
        if self.pos >= len(self.tokens) or (expected and self.tokens[self.pos][1] != expected):
            raise ValueError(f"expected {expected}")
        self.pos += 1
        return self.tokens[self.pos - 1]

    def expr(self) -> Any:
        cond = self.or_()
        if self.peek() == "?":
            self.take("?")
            yes = self.expr()
            self.take(":")
            no = self.expr()
            if cond is UNKNOWN:
                return UNKNOWN
            return yes if cond else no
        return cond

    def or_(self) -> Any:
        left = self.and_()
        while self.peek() == "||":
            self.take()
            right = self.and_()
            left = UNKNOWN if UNKNOWN in (left, right) else bool(left or right)
        return left

    def and_(self) -> Any:
        left = self.cmp()
        while self.peek() == "&&":
            self.take()
            right = self.cmp()
            left = UNKNOWN if UNKNOWN in (left, right) else bool(left and right)
        return left

    def cmp(self) -> Any:
        left = self.unary()
        if self.peek() in ("==", "!=", "<", "<=", ">", ">="):
            op = self.take()[1]
            right = self.unary()
            if UNKNOWN in (left, right):
                return UNKNOWN
            if op in ("==", "!="):
                return (left == right) == (op == "==")
            try:
                left, right = float(left), float(right)
            except (TypeError, ValueError):
                return UNKNOWN
            return {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[op]
        return left

    def unary(self) -> Any:
        if self.peek() == "!":
            self.take()
            value = self.unary()
            return UNKNOWN if value is UNKNOWN else not value
        return self.atom()

    def atom(self) -> Any:
        kind, value = self.take()
        if kind == "num":
            return json.loads(value)
        if kind == "str":
            return json.loads(value)
        if value == "(":
            inner = self.expr()
            self.take(")")
            return inner
        if value == "[":
            items = []
            while self.peek() != "]":
                items.append(self.expr())
                if self.peek() == ",":
                    self.take()
            self.take("]")
            return items
        if kind == "name":
            if value in ("true", "false"):
                return value == "true"
            if value == "null":
                return None
            if self.peek() == "(":
                return self.call(value)
            if value.startswith("var."):
                return self.env.get(value[4:], UNKNOWN)
            if value.startswith("local.") and value[6:] in self.locals and self.depth < 8:
                return evaluate_value(self.locals[value[6:]], self.env, self.locals, self.depth + 1)
            return UNKNOWN
        raise ValueError(f"unexpected {value}")

    def call(self, name: str) -> Any:
        self.take("(")
        args = []
        while self.peek() != ")":
            args.append(self.expr())
            if self.peek() == ",":
                self.take()
        self.take(")")
        if UNKNOWN in args:
            return UNKNOWN
        try:
            if name == "tonumber" and len(args) == 1:
                return None if args[0] is None else json.loads(str(args[0]))
            if name == "tostring" and len(args) == 1:
                return None if args[0] is None else str(args[0]).lower() if isinstance(args[0], bool) else str(args[0])
            if name == "tobool" and len(args) == 1:
                return args[0] if isinstance(args[0], bool) else {"true": True, "false": False}[str(args[0])]
            if name == "jsondecode" and len(args) == 1:
                return json.loads(args[0])
            if name == "contains" and len(args) == 2 and isinstance(args[0], list):
                return args[1] in args[0]
            if name == "length" and len(args) == 1:
                return len(args[0])
        except (ValueError, KeyError, TypeError):
            return UNKNOWN
        return UNKNOWN


def evaluate(expression: str, env: dict[str, Any], locals_: dict[str, Any] | None = None, depth: int = 0) -> Any:
    """Evaluate `expression` with `env` mapping variable names to values; UNKNOWN when not decidable."""
    try:
        parser = _Parser(_tokens(expression), env, locals_ or {}, depth)
        value = parser.expr()
        return value if parser.pos == len(parser.tokens) else UNKNOWN
    except (ValueError, IndexError):
        return UNKNOWN


def evaluate_value(raw: Any, env: dict[str, Any], locals_: dict[str, Any] | None = None, depth: int = 0) -> Any:
    """Evaluate a python-hcl2 value: literals pass through, `${...}` is evaluated, interpolated strings are UNKNOWN."""
    if not isinstance(raw, str) or "${" not in raw:
        return raw
    if raw.startswith("${") and raw.endswith("}") and raw.count("${") == 1:
        return evaluate(raw[2:-1], env, locals_, depth)
    return UNKNOWN
