#!/usr/bin/env python3
"""Resolve parameterized SPICE subcircuits for Netgen LVS."""

from __future__ import annotations

import argparse
import ast
import math
import operator
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.$]*")
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_.$]*)=(.*)$")
_SI_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"((?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"(meg|[tgkmunpfa])(?:[A-Za-z]*)?",
    re.IGNORECASE,
)
_SI_MULTIPLIERS = {
    "a": 1e-18,
    "f": 1e-15,
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "meg": 1e6,
    "g": 1e9,
    "t": 1e12,
}
_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
}
_UNARY_OPERATORS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCTIONS = {
    "max": max,
    "sqrt": math.sqrt,
}


@dataclass
class Statement:
    lines: list[str]


@dataclass
class Subcircuit:
    name: str
    pins: list[str]
    defaults: dict[str, str]
    header: Statement
    body: list[Statement]
    end: Statement


@dataclass
class Instance:
    tokens: list[str]
    target_index: int
    subcircuit: Subcircuit
    overrides: dict[str, str]


@dataclass
class Variant:
    subcircuit: Subcircuit
    parameters: dict[str, str]
    key: tuple[str, tuple[tuple[str, str], ...]]
    name: str = ""
    instances: dict[int, Variant] = field(default_factory=dict)


def _statements(lines: list[str]) -> list[Statement]:
    statements: list[Statement] = []
    for line in lines:
        if line.lstrip().startswith("+") and statements:
            statements[-1].lines.append(line)
        else:
            statements.append(Statement([line]))
    return statements


def _without_inline_comment(line: str) -> str:
    brace_depth = 0
    quote: str | None = None
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "{":
            brace_depth += 1
        elif char == "}":
            brace_depth = max(0, brace_depth - 1)
        elif char in "$;" and brace_depth == 0:
            return line[:index]
    return line


def _comment_suffix(line: str) -> str:
    suffix = line[len(_without_inline_comment(line)) :]
    return (" " + suffix.lstrip()) if suffix else ""


def _token_spans(line: str) -> list[tuple[str, int, int]]:
    code = _without_inline_comment(line)
    continuation = len(code) - len(code.lstrip())
    if code[continuation : continuation + 1] == "+":
        continuation += 1
    result: list[tuple[str, int, int]] = []
    start: int | None = None
    brace_depth = 0
    paren_depth = 0
    quote: str | None = None
    for index in range(continuation, len(code)):
        char = code[index]
        if start is None:
            if char.isspace():
                continue
            start = index
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "{":
            brace_depth += 1
        elif char == "}":
            brace_depth = max(0, brace_depth - 1)
        elif char == "(":
            paren_depth += 1
        elif char == ")":
            paren_depth = max(0, paren_depth - 1)
        elif char.isspace() and brace_depth == 0 and paren_depth == 0:
            result.append((line[start:index], start, index))
            start = None
    if start is not None:
        result.append((line[start : len(code)].rstrip(), start, len(code.rstrip())))
    return result


def _statement_tokens(
    statement: Statement,
) -> list[tuple[str, int, int, int]]:
    return [
        (token, line_index, start, end)
        for line_index, line in enumerate(statement.lines)
        for token, start, end in _token_spans(line)
    ]


def _edit_statement(
    statement: Statement,
    remove_parameters: bool = False,
    replacements: dict[int, str] | None = None,
) -> list[str]:
    replacements = replacements or {}
    token_spans = _statement_tokens(statement)
    edits: dict[int, list[tuple[int, int, str]]] = {}
    for token_index, (token, line_index, start, end) in enumerate(token_spans):
        replacement = replacements.get(token_index)
        if replacement is not None:
            edits.setdefault(line_index, []).append((start, end, replacement))
        elif remove_parameters and (
            token.casefold() == "params:" or _ASSIGNMENT.match(token)
        ):
            start_to_remove = start - 1 if start > 0 and statement.lines[line_index][start - 1].isspace() else start
            edits.setdefault(line_index, []).append((start_to_remove, end, ""))
    output = statement.lines[:]
    for line_index, line_edits in edits.items():
        line = output[line_index]
        for start, end, replacement in sorted(line_edits, reverse=True):
            line = line[:start] + replacement + line[end:]
        output[line_index] = line
    return output


def _parse_assignments(tokens: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for token in tokens:
        if token.casefold() == "params:":
            continue
        match = _ASSIGNMENT.match(token)
        if match:
            result[match.group(1).casefold()] = match.group(2)
    return result


def _parse_subcircuit_header(
    statement: Statement,
) -> tuple[str, list[str], dict[str, str]]:
    tokens = [token for token, _, _, _ in _statement_tokens(statement)]
    if len(tokens) < 2:
        raise ValueError(f"Malformed .subckt declaration: {line}")
    name = tokens[1]
    remainder = tokens[2:]
    first_parameter = len(remainder)
    for index, token in enumerate(remainder):
        if token.casefold() == "params:" or _ASSIGNMENT.match(token):
            first_parameter = index
            break
    pins = remainder[:first_parameter]
    defaults = _parse_assignments(remainder[first_parameter:])
    return name, pins, defaults


def _parse_netlist(
    text: str,
) -> tuple[list[Statement | Subcircuit], dict[str, Subcircuit]]:
    items: list[Statement | Subcircuit] = []
    definitions: dict[str, Subcircuit] = {}
    statements = _statements(text.splitlines())
    index = 0
    while index < len(statements):
        statement = statements[index]
        tokens = [token for token, _, _, _ in _statement_tokens(statement)]
        if not tokens or tokens[0].casefold() != ".subckt":
            items.append(statement)
            index += 1
            continue

        name, pins, defaults = _parse_subcircuit_header(statement)
        body: list[Statement] = []
        index += 1
        while index < len(statements):
            end_tokens = [
                token for token, _, _, _ in _statement_tokens(statements[index])
            ]
            if end_tokens and end_tokens[0].casefold() == ".ends":
                end = statements[index]
                index += 1
                break
            body.append(statements[index])
            index += 1
        else:
            raise ValueError(f"Subcircuit {name!r} is missing .ends")

        definition = Subcircuit(name, pins, defaults, statement, body, end)
        folded_name = name.casefold()
        if folded_name in definitions:
            raise ValueError(f"Duplicate subcircuit definition: {name}")
        definitions[folded_name] = definition
        items.append(definition)
    return items, definitions


def _numeric_value(expression: str) -> int | float | None:
    expression = expression.strip()
    if expression.startswith("{") and expression.endswith("}"):
        expression = expression[1:-1].strip()
    expression = expression.replace("^", "**")

    def replace_si_number(match: re.Match[str]) -> str:
        number, suffix = match.groups()
        multiplier = _SI_MULTIPLIERS[suffix.casefold()]
        return f"({number}*{multiplier!r})"

    expression = _SI_NUMBER.sub(replace_si_number, expression)
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return None

    def evaluate(node: ast.AST) -> int | float:
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
            return _BINARY_OPERATORS[type(node.op)](evaluate(node.left), evaluate(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
            return _UNARY_OPERATORS[type(node.op)](evaluate(node.operand))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id.casefold() in _FUNCTIONS
            and not node.keywords
        ):
            function = _FUNCTIONS[node.func.id.casefold()]
            return function(*(evaluate(argument) for argument in node.args))
        raise ValueError

    try:
        return evaluate(tree)
    except (ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None


def _format_number(value: int | float) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return format(value, ".15g")


def _resolve_value(value: str, parameters: dict[str, str]) -> str:
    value = value.strip()
    if value.startswith("{") and value.endswith("}"):
        expression = value[1:-1]
        had_braces = True
    elif (
        len(value) >= 2
        and value[0] in "'\""
        and value[-1] == value[0]
    ):
        expression = value[1:-1]
        had_braces = True
    else:
        expression = value
        had_braces = False

    def replace_identifier(match: re.Match[str]) -> str:
        resolved = parameters.get(match.group(0).casefold())
        if resolved is None:
            return match.group(0)
        numeric = _numeric_value(resolved)
        return _format_number(numeric) if numeric is not None else resolved

    expression = _IDENTIFIER.sub(replace_identifier, expression)
    numeric = _numeric_value(expression)
    if numeric is not None:
        return _format_number(numeric)
    if had_braces:
        return "{" + expression + "}"
    return expression


def _parameter_values(
    definition: Subcircuit,
    overrides: dict[str, str],
    caller_parameters: dict[str, str],
) -> dict[str, str]:
    unknown = set(overrides) - set(definition.defaults)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"Unknown parameter(s) for subcircuit {definition.name}: {names}")

    raw_values = dict(definition.defaults)
    raw_values.update(overrides)
    resolved: dict[str, str] = {}
    remaining = list(raw_values)
    local_names = set(raw_values)
    while remaining:
        newly_resolved: list[str] = []
        for name in remaining:
            dependencies = {
                match.group(0).casefold()
                for match in _IDENTIFIER.finditer(raw_values[name])
            } & local_names
            if dependencies - resolved.keys():
                continue
            scope = dict(caller_parameters)
            scope.update(resolved)
            resolved[name] = _resolve_value(raw_values[name], scope)
            newly_resolved.append(name)
        if not newly_resolved:
            break
        remaining = [name for name in remaining if name not in newly_resolved]

    for name in remaining:
        scope = dict(caller_parameters)
        scope.update(resolved)
        resolved[name] = _resolve_value(raw_values[name], scope)
    return resolved


def _parse_instance(
    statement: Statement,
    definitions: dict[str, Subcircuit],
) -> Instance | None:
    tokens = [token for token, _, _, _ in _statement_tokens(statement)]
    if not tokens or not tokens[0].casefold().startswith("x"):
        return None
    first_parameter = next(
        (
            index
            for index, token in enumerate(tokens[1:], start=1)
            if token.casefold() == "params:" or _ASSIGNMENT.match(token)
        ),
        len(tokens),
    )
    target_index = first_parameter - 1
    if target_index < 1:
        return None
    definition = definitions.get(tokens[target_index].casefold())
    if definition is None:
        return None
    overrides = _parse_assignments(tokens[target_index + 1 :])
    return Instance(tokens, target_index, definition, overrides)


def _normalized_value(value: str) -> str:
    numeric = _numeric_value(value)
    return _format_number(numeric) if numeric is not None else value.strip().casefold()


def _substitute_body_line(line: str, parameters: dict[str, str]) -> str:
    code = _without_inline_comment(line)
    edits: list[tuple[int, int, str]] = []
    for token, start, end in _token_spans(code):
        assignment = _ASSIGNMENT.match(token)
        if assignment:
            value_start = start + len(assignment.group(1)) + 1
            value = assignment.group(2)
            is_expression = (
                value.startswith(("{", "'", '"'))
                or re.search(r"[()+*/^-]", value) is not None
            )
            resolved = _resolve_value(value, parameters) if is_expression else value
            if resolved != value:
                edits.append((value_start, end, resolved))
        elif token.casefold() in parameters:
            edits.append((start, end, _resolve_value(token, parameters)))
        elif (
            token.startswith("{")
            or token.startswith("'")
            or token.startswith('"')
        ):
            resolved = _resolve_value(token, parameters)
            if resolved != token:
                edits.append((start, end, resolved))

    for start, end, replacement in reversed(edits):
        code = code[:start] + replacement + code[end:]
    return code + _comment_suffix(line)


def transform_netlist(text: str) -> str:
    """Return a Netgen-ready netlist with parameterized subcircuits resolved."""
    items, definitions = _parse_netlist(text)
    variants: dict[tuple[str, tuple[tuple[str, str], ...]], Variant] = {}
    top_instances: dict[int, Variant] = {}
    referenced_definitions: set[str] = set()

    def make_variant(
        definition: Subcircuit,
        overrides: dict[str, str],
        caller_parameters: dict[str, str],
    ) -> Variant:
        parameters = _parameter_values(definition, overrides, caller_parameters)
        signature = tuple(
            (name, _normalized_value(parameters[name]))
            for name in definition.defaults
        )
        key = (definition.name.casefold(), signature)
        existing = variants.get(key)
        if existing is not None:
            return existing
        variant = Variant(definition, parameters, key)
        variants[key] = variant

        for line_index, body_line in enumerate(definition.body):
            instance = _parse_instance(body_line, definitions)
            if instance is None:
                continue
            child = make_variant(
                instance.subcircuit,
                instance.overrides,
                variant.parameters,
            )
            variant.instances[line_index] = child
        return variant

    for item_index, item in enumerate(items):
        if isinstance(item, Subcircuit):
            continue
        instance = _parse_instance(item, definitions)
        if instance is None:
            continue
        referenced_definitions.add(instance.subcircuit.name.casefold())
        top_instances[item_index] = make_variant(
            instance.subcircuit,
            instance.overrides,
            {},
        )

    for definition in definitions.values():
        for statement in definition.body:
            instance = _parse_instance(statement, definitions)
            if instance is not None:
                referenced_definitions.add(instance.subcircuit.name.casefold())
    for definition in definitions.values():
        if definition.name.casefold() not in referenced_definitions:
            make_variant(definition, {}, {})

    used_names = set(definitions)
    variants_by_definition: dict[str, list[Variant]] = {}
    for variant in variants.values():
        variants_by_definition.setdefault(variant.subcircuit.name.casefold(), []).append(variant)

    for group in variants_by_definition.values():
        group.sort(key=lambda variant: variant.key[1])
        if len(group) == 1:
            group[0].name = group[0].subcircuit.name
            continue
        suffix = 1
        for variant in group:
            while True:
                candidate = f"{variant.subcircuit.name}_{suffix}"
                suffix += 1
                if candidate.casefold() not in used_names:
                    used_names.add(candidate.casefold())
                    variant.name = candidate
                    break

    output: list[str] = []

    def render_variant(variant: Variant) -> list[str]:
        definition = variant.subcircuit
        result = _edit_statement(
            definition.header,
            remove_parameters=True,
            replacements={1: variant.name},
        )
        for body_index, body_statement in enumerate(definition.body):
            instance = _parse_instance(body_statement, definitions)
            child = variant.instances.get(body_index)
            if instance is not None and child is not None:
                replacements = (
                    {instance.target_index: child.name}
                    if child.name.casefold() != instance.subcircuit.name.casefold()
                    else {}
                )
                result.extend(
                    _edit_statement(
                        body_statement,
                        remove_parameters=bool(instance.subcircuit.defaults),
                        replacements=replacements,
                    )
                )
            elif _statement_tokens(body_statement) and _statement_tokens(body_statement)[0][0].casefold().startswith("x"):
                result.extend(
                    _substitute_body_line(line, variant.parameters)
                    for line in body_statement.lines
                )
            else:
                for body_line in body_statement.lines:
                    result.append(
                        _substitute_body_line(body_line, variant.parameters)
                    )
        end_tokens = _statement_tokens(definition.end)
        if len(end_tokens) > 1:
            result.extend(
                _edit_statement(definition.end, replacements={1: variant.name})
            )
        else:
            result.extend(definition.end.lines)
        return result

    for item_index, item in enumerate(items):
        if isinstance(item, Subcircuit):
            group = variants_by_definition.get(item.name.casefold())
            if group:
                for variant in group:
                    output.extend(render_variant(variant))
            else:
                output.extend(item.header.lines)
                for statement in item.body:
                    output.extend(statement.lines)
                output.extend(item.end.lines)
            continue

        variant = top_instances.get(item_index)
        if variant is None:
            output.extend(item.lines)
            continue
        instance = _parse_instance(item, definitions)
        if instance is None:
            output.extend(item.lines)
            continue
        replacements = (
            {instance.target_index: variant.name}
            if variant.name.casefold() != instance.subcircuit.name.casefold()
            else {}
        )
        output.extend(
            _edit_statement(
                item,
                remove_parameters=bool(instance.subcircuit.defaults),
                replacements=replacements,
            )
        )

    return "\n".join(output).rstrip() + ("\n" if output else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", default="-", help="input netlist (default: stdin)")
    parser.add_argument("-o", "--output", help="output netlist (default: stdout)")
    args = parser.parse_args(argv)

    try:
        if args.input == "-":
            text = sys.stdin.read()
        else:
            text = Path(args.input).read_text(encoding="utf-8")
        result = transform_netlist(text)
        if args.output:
            Path(args.output).write_text(result, encoding="utf-8")
        else:
            sys.stdout.write(result)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
