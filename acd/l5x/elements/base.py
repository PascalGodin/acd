# Auto-split from the former acd/l5x/elements.py -- see CLAUDE.md's structural-cleanup notes.
import html
from dataclasses import dataclass
from sqlite3 import Cursor
from typing import List

# Characters that are illegal in XML 1.0: everything outside
# #x9 | #xA | #xD | #x20-#xD7FF | #xE000-#xFFFD | #x10000-#x10FFFF.
import re as _re
_XML_ILLEGAL_RE = _re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _escape_xml_attr(value: object) -> str:
    """Escape a value for use inside a double-quoted XML attribute.

    Beyond ``html.escape``, this strips characters that are illegal in XML 1.0
    and encodes the legal-but-delimiting whitespace (TAB/CR/LF) as numeric
    character references. Some binary records decode to garbage when an offset
    drifts (e.g. an AOI ``Vendor`` field read with ``errors="replace"``); without
    this, those raw control characters and newlines land verbatim in an
    attribute value, producing non-well-formed L5X that breaks downstream XML
    parsers.
    """
    text = _XML_ILLEGAL_RE.sub("", str(value))
    text = html.escape(text, quote=True)
    return text.replace("\t", "&#x9;").replace("\r", "&#xD;").replace("\n", "&#xA;")


def _sanitize_xml_text(text: str) -> str:
    """Encode characters illegal in XML 1.0 as XML character references (&#xNN;).

    XML 1.0 allows only #x9, #xA, #xD, and #x20-#xD7FF, #xE000-#xFFFD.
    Control characters outside that range (e.g. #x02 STX) are emitted as
    character references rather than stripped, matching Logix Designer output.

    Moved here (out of Tag, where it lived as a @staticmethod) so both
    `Tag.to_xml()` (model.py) and the L5K/Decorated comment renderers
    (rendering.py) can share one implementation without a model<->rendering
    import cycle -- rendering.py needs this for `_string_literal_cdata()`/
    `_build_comments_xml()`, and model.py already depends on rendering.py,
    not the other way around.
    """
    parts = []
    for ch in text:
        cp = ord(ch)
        if ch in ("\t", "\n", "\r") or (0x20 <= cp <= 0xD7FF) or (0xE000 <= cp <= 0xFFFD):
            parts.append(ch)
        else:
            parts.append(f"&#x{cp:04X};")
    return "".join(parts)


def _multiline_xml_text(raw: str) -> str:
    """Normalize line endings for an XML/CDATA text block, PRESERVING
    multi-line structure.

    Used for <Description>/<RevisionNote> element content in to_xml()
    renderers. Verified against a real Studio 5000 export: a tag's
    <Description> was genuinely multi-line CDATA ("Program \nBit \nFlags",
    3 lines), not collapsed to one line -- confirmed as a real discrepancy
    (not just a cosmetic difference) when Studio 5000's own Import Routine
    flagged our collapsed single-line version as different from the
    project's existing (correctly multi-line) description for the same tag.

    This is deliberately separate from the `.description` Python property
    (which DOES collapse multi-line text to one line with spaces, e.g.
    Member.description/Tag.description) -- that collapsing is documented,
    existing behavior for the convenience Python API and must not change;
    only the XML rendering needed fixing.

    Does NOT strip leading/trailing whitespace or blank lines -- a prior
    version called .strip() here, which silently discarded genuine trailing
    blank lines from a real project's own UDT description ("Timing":
    "Lug distances and go points\r\n\r\n\r\n", i.e. two real blank lines
    intentionally left at the end). Invisible to the eye (a rendered
    description with or without trailing blank lines reads the same), but a
    real Studio 5000 "Import Routine" comparison flagged the stripped
    version as different from the project's own -- caught during the first
    live end-to-end routine-carrier import test (see CLAUDE.md "Native-
    import escape hatches").
    """
    return raw.replace("\r\n", "\n").replace("\r", "\n")


def _validate_rll_rung_syntax(text: str) -> None:
    """Catch a narrow but real class of malformed RLL rung text before it
    round-trips through export -> Studio 5000 import: unbalanced
    parentheses/brackets, and a `"[...]"` branch group left with only one
    member after an edit removed a sibling branch but left the brackets
    behind. In this ASCII RLL dialect `"[...]"` means "parallel branches"
    and needs >= 2 comma-separated members -- a single remaining branch
    should have no brackets at all.

    Found via a real downstream-agent failure: neither
    `ProjectDB.replace_rung_safe()`/`db_replace_rung_safe()` (which only
    guards that you're editing the rung you think you are, not its
    grammar) nor `export_routine(validate=True)` (which walks tag
    DataType trees for resolvability -- a data-shape check, unrelated to
    RLL syntax) caught `"[MOVE(...) FOR(...) ]"` (a one-branch bracket
    left over after removing its sibling). Only Studio 5000's own import
    parser did, after a full edit -> export -> import round trip.

    Deliberately narrow, NOT a real ladder-logic grammar checker (see
    CLAUDE.md) -- just the two cheapest, lowest-false-positive checks
    available without one:
    - Unbalanced `(`/`)`/`[`/`]`.
    - A branch `"[...]"` group with fewer than 2 top-level (not nested)
      comma-separated members.

    A `[` immediately following an identifier/`.` character (e.g.
    `"MyArray[5]"`, `"MyArray[2,2,1]"`) is treated as an array-index
    operand, not a branch group, and is excluded from the branch-
    multiplicity check -- conflating the two would misclassify a
    perfectly valid single-element array index as an invalid one-branch
    group. Single-quoted string literals (Rockwell's own `"$'"`-escaped
    convention, see `_l5k_string_padded`) are skipped whole, so
    punctuation inside a STRING tag's literal value can never trip this.

    Raises ValueError naming the specific problem and its character
    position; a no-op for empty/whitespace-only text.
    """
    if not text or not text.strip():
        return
    stack: List[dict] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            j = i + 1
            closed = False
            while j < n:
                if text[j] == "$" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == "'":
                    j += 1
                    closed = True
                    break
                j += 1
            if not closed:
                raise ValueError(
                    f"Unterminated string literal starting at position {i} in rung text: {text!r}"
                )
            i = j
            continue
        if ch == "(":
            stack.append({"kind": "paren", "commas": 0, "pos": i})
        elif ch == ")":
            if not stack or stack[-1]["kind"] != "paren":
                raise ValueError(f"Unbalanced ')' at position {i} in rung text: {text!r}")
            stack.pop()
        elif ch == "[":
            prev = text[i - 1] if i > 0 else ""
            is_index = prev.isalnum() or prev in ("_", ".")
            stack.append({"kind": "index" if is_index else "branch", "commas": 0, "pos": i})
        elif ch == "]":
            if not stack or stack[-1]["kind"] not in ("branch", "index"):
                raise ValueError(f"Unbalanced ']' at position {i} in rung text: {text!r}")
            frame = stack.pop()
            if frame["kind"] == "branch" and frame["commas"] == 0:
                inner = text[frame["pos"] + 1:i]
                member_count = 0 if not inner.strip() else 1
                raise ValueError(
                    f"'[...]' branch group at position {frame['pos']} has only "
                    f"{member_count} member(s) (no top-level comma) -- a parallel-branch "
                    f"group needs >= 2 comma-separated members, or should be written "
                    f"without brackets at all if only one remains: {text!r}"
                )
        elif ch == ",":
            if stack:
                stack[-1]["commas"] += 1
        i += 1
    if stack:
        frame = stack[-1]
        opener = "(" if frame["kind"] == "paren" else "["
        raise ValueError(f"Unbalanced {opener!r} at position {frame['pos']} in rung text: {text!r}")


@dataclass
class L5xElementBuilder:
    _cur: Cursor
    _object_id: int = -1


# Maps Python attribute names to L5X XML section wrapper tag names.
# Entries here also control which list attributes are serialized as child sections.
_LIST_SECTION_NAMES = {
    "tags": "Tags",
    "local_tags": "LocalTags",
    "parameters": "Parameters",
    "data_types": "DataTypes",
    "members": "Members",
    "modules": "Modules",
    "programs": "Programs",
    "routines": "Routines",
    "aois": "AddOnInstructionDefinitions",
    "tasks": "Tasks",
    "scheduled_programs": "ScheduledPrograms",
}

@dataclass
class L5xElement:
    _name: str

    def __post_init__(self):
        self._export_name = ""

    def to_xml(self) -> str:
        attribute_list: List[str] = []
        child_list: List[str] = []
        for attribute in self.__dict__:
            if attribute[0] != "_":
                attribute_value = self.__getattribute__(attribute)
                if attribute_value is None:
                    continue
                if isinstance(attribute_value, L5xElement):
                    child_list.append(attribute_value.to_xml())
                elif isinstance(attribute_value, list):
                    if attribute in _LIST_SECTION_NAMES:
                        section_name = _LIST_SECTION_NAMES[attribute]
                        new_child_list: List[str] = []
                        for element in attribute_value:
                            if isinstance(element, L5xElement):
                                if getattr(element, "_l5x_exclude", False):
                                    continue
                                new_child_list.append(element.to_xml())
                            else:
                                new_child_list.append(f"<{element}/>")
                        child_list.append(
                            f'<{section_name}>{"".join(new_child_list)}</{section_name}>'
                        )
                else:
                    if attribute == "cls":
                        attribute = "class"
                    if isinstance(attribute_value, bool):
                        attribute_value = str(attribute_value).lower()
                    _overrides = getattr(self, "_xml_attr_overrides", {})
                    xml_attr_name = _overrides.get(attribute, attribute.title().replace("_", ""))
                    attribute_list.append(
                        f'{xml_attr_name}="{_escape_xml_attr(attribute_value)}"'
                    )

        _export_name = (
            getattr(self, "_export_name", "") or self.__class__.__name__.title().replace("_", "")
        )
        return f'<{_export_name} {" ".join(attribute_list)}>{"".join(child_list)}</{_export_name}>'
