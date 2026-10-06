"""Compare acd-tools' partial L5X exports against Studio 5000's own, using
Rockwell's Logix Designer SDK -- a DEV tool for finding export gaps.

acd-tools itself never imports or requires the SDK; only this script does.

For every routine, program and AOI of a project, it:
  1. exports it with acd-tools (export_routine/export_program/export_aoi),
  2. has Studio export the same object (SDK partial_export_to_xml_file),
  3. diffs the two as XML trees, ignoring differences already known and
     accepted (see _normalize()), and
  4. optionally imports acd-tools' file into a scratch copy of the project
     (SDK partial_import_from_xml_file, never saved) and records Studio's
     own import warnings/errors.

The source project is never opened by the SDK: everything runs on copies
inside OUTDIR.

Requirements: the Logix Designer SDK installed with a Studio 5000
Professional license, and Python 3.12/3.13 with both acd-tools and the SDK's
`logix_designer_sdk` wheel installed (e.g. a separate venv:
`py -3.13 -m venv .venv`, then `pip install -e <acd repo>` and
`pip install "<SDK dir>\\python\\logix_designer_sdk-*.whl"`).

Usage:
    python scripts/sdk_compare.py PROJECT.ACD OUTDIR [--program NAME]
        [--kinds routine,program,aoi] [--limit N] [--no-import] [--diff-only]

Writes OUTDIR/report.md (summary by difference category) and
OUTDIR/report.json (every difference and import message per object).
"""
import argparse
import asyncio
import json
import re
import shutil
import sys
import traceback
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

try:
    from logix_designer_sdk import ImportCollisionOptions, LogixProject
    from logix_designer_sdk.logging import OperationEvent
except ImportError:
    sys.exit(
        "logix_designer_sdk is not installed in this interpreter -- see this "
        "script's docstring (needs Python 3.12/3.13 and the SDK wheel)."
    )

from acd.api import export_aoi, export_program, export_routine, load_acd

# Attributes that legitimately differ per export run, or are known,
# accepted differences (see CLAUDE.md "GSV/SSV objects and
# MESSAGE-configured tags as export context").
# SoftwareRevision: the SDK opens a project in the installed Studio version
# and stamps that on its exports. DataExchangeId: a per-tag GUID Studio adds
# in its own export, like OpcUaAccess.
IGNORED_ATTRS = {
    "ExportDate", "Owner", "ExportOptions", "OpcUaAccess", "SoftwareRevision", "DataExchangeId",
}

# Containers whose children are an unordered set of named objects (Studio
# sorts some alphabetically, acd-tools uses project order -- not meaningful).
UNORDERED_CONTAINERS = {
    "DataTypes", "Modules", "AddOnInstructionDefinitions", "Tags", "Tasks",
    "Dependencies", "Programs", "LocalTags", "Comments",
}


# Log lines from the import pass. Note: the subclass must NOT set its own
# __namespace__ -- with one, open_logix_project() fails inside the SDK
# ("Exception has been thrown by the target of an invocation").
_LOG_LINES = []


class _Collector(OperationEvent):
    def log_status_message(self, project_file, msg):
        _LOG_LINES.append(str(msg))

    def log_error_message(self, project_file, msg):
        _LOG_LINES.append("ERROR " + str(msg))


# --- XML normalization and diff -------------------------------------------

def _normalize(elem):
    """Drop what is known and accepted to differ, in place."""
    for child in list(elem):
        if child.tag == "DataType" and child.get("Class") in ("ProductDefined", "IO"):
            elem.remove(child)  # the SDK export adds these (ProductDefinedTypes); the GUI export doesn't
        elif child.tag == "Tag" and ":" in (child.get("Name") or ""):
            elem.remove(child)  # I/O tags: the SDK export adds them (IOTags); the GUI export doesn't
        elif child.tag == "DefaultData":
            elem.remove(child)  # AOI DefaultData: parked, see CLAUDE.md
        elif elem.tag == "Tag" and elem.get("DataType") == "MESSAGE" and child.tag == "Data":
            elem.remove(child)  # MESSAGE <Data Format="Message">: not rendered
        else:
            _normalize(child)
    if elem.tag in ("Data", "DefaultData") and elem.get("Format") == "L5K" and elem.text:
        # Studio wraps long L5K values with "\n\t\t"; raw whitespace is never
        # part of the value (inside a string literal it would be $N/$T/$XX).
        elem.text = re.sub(r"[\r\n\t]", "", elem.text)
    for attr in IGNORED_ATTRS:
        elem.attrib.pop(attr, None)


def _label(elem):
    name = elem.get("Name")
    if name is None and elem.tag in ("Rung", "Line"):
        name = elem.get("Number")
    if name is None and elem.tag in ("Data", "DefaultData"):
        name = elem.get("Format")
    if name is None and elem.tag == "Comment":
        name = elem.get("Operand")
    if name is None and elem.tag == "Element":
        name = elem.get("Index")
    return f"{elem.tag}[{name}]" if name is not None else elem.tag


def _text(elem):
    return (elem.text or "").strip()


def _diff(a, b, path, out):
    """Studio (a) vs acd-tools (b). Appends (category, detail) tuples."""
    # path with names stripped, for grouping (a name can itself contain
    # brackets, e.g. Element[[0,1]] or Comment[[3].5])
    gpath = re.sub(r"\[(?:[^\[\]]|\[[^\]]*\])*\]", "", path)
    for k in sorted(set(a.attrib) | set(b.attrib)):
        va, vb = a.get(k), b.get(k)
        if va != vb:
            out.append((f"attr {gpath}@{k}", f"{path}@{k}: studio={va!r} ours={vb!r}"))
    if _text(a) != _text(b):
        out.append((f"text {gpath}", f"{path}: studio={_text(a)[:120]!r} ours={_text(b)[:120]!r}"))

    if a.tag in UNORDERED_CONTAINERS:
        ka = {_label(c): c for c in a}
        kb = {_label(c): c for c in b}
        for key in ka.keys() - kb.keys():
            out.append((f"missing {gpath}/{ka[key].tag}", f"{path}/{key}"))
        for key in kb.keys() - ka.keys():
            out.append((f"extra {gpath}/{kb[key].tag}", f"{path}/{key}"))
        pairs = [(ka[k], kb[k]) for k in ka.keys() & kb.keys()]
    else:
        la, lb = list(a), list(b)
        sa, sb = [_label(c) for c in la], [_label(c) for c in lb]
        if sa != sb:
            for key in [x for x in sa if x not in sb]:
                out.append((f"missing {gpath}/{key.split('[')[0]}", f"{path}/{key}"))
            for key in [x for x in sb if x not in sa]:
                out.append((f"extra {gpath}/{key.split('[')[0]}", f"{path}/{key}"))
            common = [x for x in sa if x in sb]
            if common != [x for x in sb if x in sa]:
                out.append((f"order {gpath}", f"{path}: studio={sa} ours={sb}"))
        used = set()
        pairs = []
        for ca, la_key in zip(la, sa):
            for j, (cb, lb_key) in enumerate(zip(lb, sb)):
                if j not in used and lb_key == la_key:
                    used.add(j)
                    pairs.append((ca, cb))
                    break
    for ca, cb in pairs:
        _diff(ca, cb, f"{path}/{_label(ca)}", out)


def compare_files(studio_path, ours_path):
    a = ET.parse(studio_path).getroot()
    b = ET.parse(ours_path).getroot()
    _normalize(a)
    _normalize(b)
    out = []
    _diff(a, b, a.tag, out)
    return out


# --- import log parsing ----------------------------------------------------

_EXC_RE = re.compile(
    r"<Exception Severity='(\w+)'.*?<!\[CDATA\[(.*?)\]\]>.*?<Target>\s*(.*?)\s*</Target>", re.S
)


def parse_import_log(lines):
    text = "\n".join(l[len("ImportLog "):] if l.startswith("ImportLog ") else l for l in lines)
    msgs = [{"severity": s, "message": m.strip(), "target": t.strip()} for s, m, t in _EXC_RE.findall(text)]
    msgs += [{"severity": "Error", "message": l[6:], "target": ""} for l in lines if l.startswith("ERROR ")]
    return msgs


# --- main ------------------------------------------------------------------

def _safe(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def collect_objects(project, kinds, program_filter, limit):
    objs = []
    for prog in project.controller.programs:
        if program_filter and prog.name != program_filter:
            continue
        if "program" in kinds:
            objs.append(("program", prog.name, f"Controller/Programs/Program[@Name='{prog.name}']",
                         lambda path, p=prog: export_program(project, p, path)))
        if "routine" in kinds:
            for r in prog.routines:
                if r.type not in ("RLL", "ST"):
                    continue  # FBD/SFC content isn't decoded (known gap)
                objs.append(("routine", f"{prog.name}/{r.name}",
                             f"Controller/Programs/Program[@Name='{prog.name}']/Routines/Routine[@Name='{r.name}']",
                             lambda path, rr=r: export_routine(project, rr, path)))
    if "aoi" in kinds and not program_filter:
        for aoi in project.controller.aois:
            objs.append(("aoi", aoi.name,
                         f"Controller/AddOnInstructionDefinitions/AddOnInstructionDefinition[@Name='{aoi.name}']",
                         lambda path, a=aoi: export_aoi(project, a, path)))
    return objs[:limit] if limit else objs


def rediff(out):
    """Re-run only the diff over a previous run's files (after changing the
    comparer's normalization), keeping its import results."""
    results = json.loads((out / "report.json").read_text(encoding="utf-8"))
    for res in results.values():
        res.pop("diffs", None)
        res.pop("diff_error", None)
        if "ours" in res and "studio" in res:
            try:
                res["diffs"] = compare_files(res["studio"], res["ours"])
            except Exception:  # noqa: BLE001
                res["diff_error"] = traceback.format_exc(limit=2)
    write_reports(out, results)


async def run(args):
    out = Path(args.outdir).resolve()
    if args.diff_only:
        rediff(out)
        return
    (out / "ours").mkdir(parents=True, exist_ok=True)
    (out / "studio").mkdir(parents=True, exist_ok=True)
    work = out / "work_export.ACD"
    shutil.copyfile(args.acd, work)

    project = load_acd(str(work))
    objs = collect_objects(project, set(args.kinds.split(",")), args.program, args.limit)
    print(f"{len(objs)} objects to compare")

    results = {}
    for kind, name, xpath, export in objs:
        key = f"{kind}:{name}"
        res = results[key] = {"kind": kind, "name": name, "xpath": xpath}
        ours = out / "ours" / f"{kind}__{_safe(name)}.L5X"
        try:
            export(str(ours))
            res["ours"] = str(ours)
        except Exception as e:  # noqa: BLE001 -- report every failure, keep going
            res["ours_error"] = f"{type(e).__name__}: {e}"

    sdk = await LogixProject.open_logix_project(str(work))
    try:
        for key, res in results.items():
            studio = out / "studio" / f"{res['kind']}__{_safe(res['name'])}.L5X"
            studio.unlink(missing_ok=True)  # the SDK refuses to overwrite
            try:
                await sdk.partial_export_to_xml_file(res["xpath"], str(studio))
                res["studio"] = str(studio)
            except Exception as e:  # noqa: BLE001
                res["studio_error"] = f"{type(e).__name__}: {e}"
    finally:
        sdk.close()

    for res in results.values():
        if "ours" in res and "studio" in res:
            try:
                res["diffs"] = compare_files(res["studio"], res["ours"])
            except Exception:  # noqa: BLE001
                res["diff_error"] = traceback.format_exc(limit=2)

    if not args.no_import:
        target = out / "work_import.ACD"
        shutil.copyfile(args.acd, target)
        sdk = await LogixProject.open_logix_project(str(target), _Collector())
        try:
            for res in results.values():
                if "ours" not in res:
                    continue
                _LOG_LINES.clear()
                try:
                    await sdk.partial_import_from_xml_file(
                        res["xpath"], res["ours"], ImportCollisionOptions.OVERWRITE_ON_COLL
                    )
                except Exception as e:  # noqa: BLE001
                    res["import_exception"] = f"{type(e).__name__}: {e}"
                # The import log is delivered asynchronously and can arrive
                # after the await returns -- wait for its closing line, or the
                # messages land on the next object.
                for _ in range(100):
                    if any("</ImportLog>" in line for line in _LOG_LINES):
                        break
                    await asyncio.sleep(0.1)
                res["import_messages"] = parse_import_log(list(_LOG_LINES))
        finally:
            sdk.close()  # never saved: the copy is left untouched on disk

    write_reports(out, results)


def write_reports(out, results):
    (out / "report.json").write_text(json.dumps(results, indent=1, default=list), encoding="utf-8")

    categories = defaultdict(list)
    for key, res in results.items():
        for cat, detail in res.get("diffs", []):
            categories[cat].append((key, detail))
    import_msgs = defaultdict(list)
    for key, res in results.items():
        for m in res.get("import_messages", []):
            import_msgs[(m["severity"], m["message"])].append((key, m["target"]))

    n = len(results)
    clean = sum(1 for r in results.values() if "diffs" in r and not r["diffs"])
    failures = [(k, r) for k, r in results.items()
                if "ours_error" in r or "studio_error" in r or "diff_error" in r or "import_exception" in r]
    lines = [
        "# acd-tools vs Studio export comparison", "",
        f"- objects: {n}",
        f"- identical after normalization: {clean}",
        f"- with differences: {sum(1 for r in results.values() if r.get('diffs'))}",
        f"- failures (export/diff/import exception): {len(failures)}", "",
    ]
    if failures:
        lines.append("## Failures")
        for k, r in failures:
            err = r.get("ours_error") or r.get("studio_error") or r.get("import_exception") or r.get("diff_error")
            lines.append(f"- `{k}`: {err.splitlines()[0][:300]}")
        lines.append("")
    lines.append("## Import messages (Studio importing acd-tools' files)")
    for (sev, msg), hits in sorted(import_msgs.items(), key=lambda kv: (kv[0][0] != "Error", -len(kv[1]))):
        lines.append(f"- **{sev}** x{len(hits)}: {msg[:200]}")
        for k, t in hits[:3]:
            lines.append(f"  - `{k}` -> `{t[:200]}`")
    lines.append("")
    lines.append("## Difference categories (studio vs ours), most objects first")
    def objs_in(entries):
        return len({k for k, _ in entries})
    for cat, entries in sorted(categories.items(), key=lambda kv: -objs_in(kv[1])):
        lines.append(f"- **{cat}** -- {objs_in(entries)} objects, {len(entries)} occurrences")
        for k, d in entries[:3]:
            lines.append(f"  - `{k}`: {d[:300]}")
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out / 'report.md'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("acd")
    ap.add_argument("outdir")
    ap.add_argument("--program", help="only this program (and its routines)")
    ap.add_argument("--kinds", default="routine,program,aoi")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-import", action="store_true")
    ap.add_argument("--diff-only", action="store_true",
                    help="re-diff OUTDIR's existing files (no export/import); ACD is ignored")
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
