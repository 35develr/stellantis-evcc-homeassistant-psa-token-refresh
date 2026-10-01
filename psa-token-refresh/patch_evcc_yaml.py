#!/usr/bin/env python3
"""Targeted patching of evcc.yaml.

Replaces only the `tokens.access` / `tokens.refresh` lines (and optionally `user`)
within the vehicle blocks, so that comments, formatting and all other settings
of the file remain untouched. A complete rewrite using a YAML parser would lose
comments and ordering - therefore line by line.

Standard library only, so it runs anywhere and is testable locally.
"""

from __future__ import annotations

import re
import time

__all__ = ["PatchError", "patch_evcc_yaml", "find_vehicle_block"]


class PatchError(Exception):
    """Vehicle block not found or file not patchable."""


# "- name: auto1"  (optionally quoted)
_LIST_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)-\s*name:\s*(?P<name>.*?)\s*$")
_KEY_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z_][\w-]*):[ \t]*(?P<value>.*?)[ \t]*$")
_TOKENS_INLINE_RE = re.compile(
    r"^(?P<indent>[ \t]*)tokens:[ \t]*\{(?P<body>.*)\}[ \t]*$"
)
_USERSUFFIX_RE = re.compile(r"^(?P<base>.*?)#(?P<gen>\d+)$")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _quote(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def find_vehicle_block(lines: list[str], name: str) -> tuple[int, int, int, int]:
    """Finds the block of a vehicle.

    Returns: (start, end, dash_indent, key_indent) - `end` is exclusive.
    """
    start = None
    dash_indent = 0
    for idx, line in enumerate(lines):
        m = _LIST_ITEM_RE.match(line.rstrip("\r\n"))
        if m and _unquote(m.group("name")) == name:
            start = idx
            dash_indent = _indent_of(m.group("indent"))
            break

    if start is None:
        raise PatchError(f"vehicle '{name}' not found as '- name: {name}'")

    # key_indent = indentation of the list entry's keys (typically dash_indent+2)
    key_indent = dash_indent + 2
    for idx in range(start + 1, len(lines)):
        raw = lines[idx].rstrip("\r\n")
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        ind = _indent_of(raw)
        if ind <= dash_indent:
            break
        key_indent = ind
        break

    end = len(lines)
    for idx in range(start + 1, len(lines)):
        raw = lines[idx].rstrip("\r\n")
        if not raw.strip():
            continue
        if _indent_of(raw) <= dash_indent:
            end = idx
            break

    return start, end, dash_indent, key_indent


def _set_scalar(lines: list[str], start: int, end: int, key: str, value: str,
                min_indent: int) -> tuple[bool, list[str]]:
    """Sets key: value in lines[start:end]; appends if not present."""
    for idx in range(start, end):
        raw = lines[idx].rstrip("\r\n")
        m = _KEY_RE.match(raw)
        if m and m.group("key") == key and _indent_of(raw) >= min_indent:
            newline = f"{m.group('indent')}{key}: {value}\n"
            if lines[idx] == newline:
                return False, lines
            lines[idx] = newline
            return True, lines

    lines.insert(start, f"{' ' * min_indent}{key}: {value}\n")
    return True, lines


def _set_tokens(lines: list[str], start: int, end: int, key_indent: int,
                access: str, refresh: str) -> tuple[list[str], list[str]]:
    """Sets tokens.access / tokens.refresh, creates the tokens block if needed."""
    notes: list[str] = []

    for idx in range(start, end):
        raw = lines[idx].rstrip("\r\n")

        inline = _TOKENS_INLINE_RE.match(raw)
        if inline and _indent_of(raw) >= key_indent:
            inner_access, inner_refresh = access, refresh
            for part in inline.group("body").split(","):
                if part.strip().startswith("access:"):
                    inner_access = _unquote(part.split(":", 1)[1])
                elif part.strip().startswith("refresh:"):
                    inner_refresh = _unquote(part.split(":", 1)[1])
            if inner_access == access and inner_refresh == refresh:
                return lines, notes
            lines[idx] = (
                f"{inline.group('indent')}tokens: {{access: {_quote(access)}, "
                f"refresh: {_quote(refresh)}}}\n"
            )
            notes.append("tokens (inline) updated")
            return lines, notes

        m = _KEY_RE.match(raw)
        if m and m.group("key") == "tokens" and _indent_of(raw) >= key_indent:
            block_start = idx + 1
            block_end = end
            for j in range(block_start, end):
                r2 = lines[j].rstrip("\r\n")
                if not r2.strip():
                    continue
                if _indent_of(r2) <= _indent_of(raw):
                    block_end = j
                    break
            token_indent = _indent_of(raw) + 2
            changed_a, lines = _set_scalar(
                lines, block_start, block_end, "access", _quote(access), token_indent
            )
            # block_end may have shifted due to an insertion
            block_end += 1 if changed_a and _indent_of(lines[block_start].rstrip()) < token_indent else 0
            changed_r, lines = _set_scalar(
                lines, block_start, block_end, "refresh", _quote(refresh), token_indent
            )
            if changed_a or changed_r:
                notes.append("tokens.access/refresh updated")
            return lines, notes

    # no tokens block present -> create after 'user' (otherwise directly under name)
    insert_at = start + 1
    for idx in range(start, end):
        m = _KEY_RE.match(lines[idx].rstrip("\r\n"))
        if m and m.group("key") == "user":
            insert_at = idx + 1
            break
    lines[insert_at:insert_at] = [
        f"{' ' * key_indent}tokens:\n",
        f"{' ' * (key_indent + 2)}access: {_quote(access)}\n",
        f"{' ' * (key_indent + 2)}refresh: {_quote(refresh)}\n",
    ]
    notes.append("tokens block created")
    return lines, notes


def patch_evcc_yaml(text: str, vehicles: list[dict], *, rotate_user_key: bool = True,
                    generation: int | None = None) -> tuple[str, list[str], list[str]]:
    """Patches evcc.yaml content.

    vehicles: [{"name": "auto1", "vin": "VF3...", "access": "...", "refresh": "..."}]
    Returns: (new_text, changes, warnings)
    """
    lines = text.splitlines(keepends=True)
    generator = generation or int(time.strftime("%y%m%d%H%M"))
    changes: list[str] = []
    warnings: list[str] = []

    for veh in vehicles:
        name = veh["name"]
        access = veh["access"]
        refresh = veh["refresh"]
        vin = veh.get("vin")

        start, end, _dash_indent, key_indent = find_vehicle_block(lines, name)

        if vin:
            found_vin = None
            for idx in range(start, end):
                m = _KEY_RE.match(lines[idx].rstrip("\r\n"))
                if m and m.group("key") == "vin":
                    found_vin = _unquote(m.group("value"))
            if found_vin and found_vin != vin:
                warnings.append(
                    f"{name}: VIN in evcc.yaml ({found_vin}) != configured VIN ({vin}) "
                    "- the block present in evcc.yaml is patched"
                )

        if rotate_user_key:
            for idx in range(start, end):
                raw = lines[idx].rstrip("\r\n")
                m = _KEY_RE.match(raw)
                if m and m.group("key") == "user":
                    current = _unquote(m.group("value"))
                    base = _USERSUFFIX_RE.match(current)
                    base = base.group("base") if base else current
                    new_user = f"{base}#{generator}"
                    if new_user != current:
                        lines[idx] = f"{m.group('indent')}user: {_quote(new_user)}\n"
                        changes.append(f"{name}: user -> {new_user} (new token key)")
                    break

        # re-determine block boundaries after any changes
        start, end, _dash_indent, key_indent = find_vehicle_block(lines, name)
        lines, notes = _set_tokens(lines, start, end, key_indent, access, refresh)
        for note in notes:
            changes.append(f"{name}: {note}")

    return "".join(lines), changes, warnings


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import sys

    with open(sys.argv[1], encoding="utf-8") as fh:
        src = fh.read()
    out, ch, wr = patch_evcc_yaml(
        src,
        [{"name": "auto1", "vin": "VF3ABC", "access": "AAA", "refresh": "RRR"}],
    )
    print(out)
    print("changes:", ch)
    print("warnings:", wr)
