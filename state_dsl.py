"""Narrow predicate evaluator for state-aware Desktop-scheduled triggers.

Schema v2's ``trigger.fire_when`` field is a small DSL evaluated by the
backend. Intentionally NOT arbitrary Python eval -- only the predicates
listed below are accepted. Anything else raises ``DslError``.

Grammar (informal):

    expr      := disjunction
    disjunction := conjunction ('OR' conjunction)*
    conjunction := atom ('AND' atom)*
    atom      := ident OP value
    OP        := '==' | '!=' | '>=' | '<=' | '>' | '<'
    ident     := 'hour' | 'last_<event>_date' (where <event> is alphanumeric)
    value     := 'today' | 'yesterday' | <integer> | 'null'

Examples:

    last_sod_date != today
    last_sod_date == today AND hour >= 17 AND last_eod_date != today
    last_sod_date != today OR last_eod_date == null

Evaluation context (always available):

    today      -> 'YYYY-MM-DD' (local date)
    yesterday  -> 'YYYY-MM-DD' (today minus 1 day)
    hour       -> 0..23 (local hour)
    last_<event>_date -> string or None from the state dict

If a ``last_*`` ident is missing from the state dict it evaluates to
``None`` (== null). Equality with 'today' / 'yesterday' compares strings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


_OPS = ("==", "!=", ">=", "<=", ">", "<")
_TOKEN_RE = re.compile(
    r"(==|!=|>=|<=|>|<|\bAND\b|\bOR\b)"
)


class DslError(ValueError):
    """Raised when fire_when can't be parsed or evaluates against bad
    state."""


@dataclass(frozen=True)
class _Atom:
    ident: str
    op: str
    value: str


def _tokenize(expr: str) -> list[tuple[str, str]]:
    """Tokenise into (kind, text). kinds: 'TERM', 'OP', 'AND', 'OR'."""
    out: list[tuple[str, str]] = []
    i = 0
    # Split on operators / AND / OR; everything else is a TERM.
    parts = _TOKEN_RE.split(expr)
    for raw in parts:
        s = raw.strip()
        if not s:
            continue
        if s in _OPS:
            out.append(("OP", s))
        elif s == "AND":
            out.append(("AND", s))
        elif s == "OR":
            out.append(("OR", s))
        else:
            out.append(("TERM", s))
    return out


def _parse_atom(tokens: list[tuple[str, str]], pos: int) -> tuple[_Atom, int]:
    if pos + 2 >= len(tokens):
        raise DslError(f"expected ident OP value at position {pos}")
    ident_tok = tokens[pos]
    op_tok = tokens[pos + 1]
    value_tok = tokens[pos + 2]
    if ident_tok[0] != "TERM":
        raise DslError(f"expected ident, got {ident_tok!r}")
    if op_tok[0] != "OP":
        raise DslError(f"expected operator, got {op_tok!r}")
    if value_tok[0] != "TERM":
        raise DslError(f"expected value, got {value_tok!r}")
    return _Atom(ident=ident_tok[1], op=op_tok[1], value=value_tok[1]), pos + 3


def _eval_atom(atom: _Atom, ctx: dict[str, Any]) -> bool:
    # Resolve ident.
    if atom.ident == "hour":
        lhs: Any = ctx["hour"]
    elif atom.ident.startswith("last_") and atom.ident.endswith("_date"):
        lhs = ctx.get(atom.ident)  # may be None
    else:
        raise DslError(
            f"unknown ident {atom.ident!r}; supported: hour, "
            "last_<event>_date"
        )

    # Resolve value.
    if atom.value == "today":
        rhs: Any = ctx["today"]
    elif atom.value == "yesterday":
        rhs = ctx["yesterday"]
    elif atom.value == "null":
        rhs = None
    else:
        try:
            rhs = int(atom.value)
        except ValueError:
            raise DslError(
                f"value {atom.value!r} not recognised; expected today, "
                "yesterday, null, or integer"
            )

    # hour comparisons must be int<->int.
    if atom.ident == "hour" and not isinstance(rhs, int):
        raise DslError(
            f"hour requires integer rhs; got {atom.value!r}"
        )
    # date comparisons: both sides should be string or None.
    if atom.ident.startswith("last_") and isinstance(rhs, int):
        raise DslError(
            f"date ident {atom.ident!r} requires today/yesterday/null "
            f"rhs; got integer {rhs}"
        )

    if atom.op == "==":
        return lhs == rhs
    if atom.op == "!=":
        return lhs != rhs
    # Order comparisons -- only meaningful for hour (or future int idents).
    if not isinstance(lhs, (int, float)) or not isinstance(rhs, (int, float)):
        raise DslError(
            f"order comparison {atom.op!r} requires integer operands; "
            f"got lhs={lhs!r}, rhs={rhs!r}"
        )
    if atom.op == ">=":
        return lhs >= rhs
    if atom.op == "<=":
        return lhs <= rhs
    if atom.op == ">":
        return lhs > rhs
    if atom.op == "<":
        return lhs < rhs
    raise DslError(f"unknown op {atom.op!r}")


def evaluate(expr: str, state: dict[str, Any],
              *, now: datetime | None = None) -> bool:
    """Evaluate a fire_when expression against the given state dict.

    ``state`` carries the workflow_state.json contents (last_*_date,
    etc.). ``now`` defaults to local-time wall clock; tests inject.
    """
    if not isinstance(expr, str) or not expr.strip():
        raise DslError("fire_when must be a non-empty string")

    now = now or datetime.now()
    ctx = {
        "today": now.strftime("%Y-%m-%d"),
        "yesterday": (now - timedelta(days=1)).strftime("%Y-%m-%d"),
        "hour": now.hour,
    }
    # Merge state's last_*_date keys into the context.
    for k, v in (state or {}).items():
        if k.startswith("last_") and k.endswith("_date"):
            ctx[k] = v

    tokens = _tokenize(expr)
    if not tokens:
        raise DslError("empty expression after tokenisation")

    # Parse: disjunction of conjunctions of atoms.
    pos = 0
    disjuncts: list[bool] = []
    conjuncts: list[bool] = []

    while pos < len(tokens):
        atom, pos = _parse_atom(tokens, pos)
        conjuncts.append(_eval_atom(atom, ctx))
        if pos >= len(tokens):
            break
        sep = tokens[pos]
        if sep[0] == "AND":
            pos += 1
            continue
        if sep[0] == "OR":
            disjuncts.append(all(conjuncts))
            conjuncts = []
            pos += 1
            continue
        raise DslError(f"unexpected token {sep!r} at position {pos}")

    if conjuncts:
        disjuncts.append(all(conjuncts))
    return any(disjuncts)
