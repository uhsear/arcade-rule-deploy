#!/usr/bin/env python
"""Deploy Arcade calculation attribute rules to a geodatabase, preflight-checked.

Rules live in a JSON file. The default run checks and writes nothing. Adding
rules needs --apply, and it is idempotent: a rule of the same name is removed
before it is added, so re-running does not stack duplicates.

The check that earns this tool: every FeatureSetByName("...") reference inside
every script is resolved against the target workspace. ArcGIS accepts a rule
whose referenced layer does not exist. Nothing complains at add time. The rule
then returns empty at run time, quietly writing wrong values into production.

    python arcade_rule_deploy.py --self-test
    python arcade_rule_deploy.py --rules rules.json --workspace prod.sde
    python arcade_rule_deploy.py --rules rules.json --workspace prod.sde --apply
    python arcade_rule_deploy.py --rules rules.json --workspace prod.sde --verify

Exit codes: 0 ok, 1 preflight or verify failed, 2 apply partially failed,
64 usage error.
"""

from __future__ import print_function

import argparse
import json
import os
import re
import sys

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# Rule type this tool manages. Calculation rules compute a field value.
# Constraint and validation rules have different parameters and are out of scope.
RULE_TYPE = "CALCULATION"

# Triggering events ArcGIS accepts for a calculation rule.
VALID_TRIGGERS = ("INSERT", "UPDATE", "DELETE")

# Arcade functions that name a dataset as a string literal. Every name found
# through one of these is resolved against the workspace during preflight.
DATASET_REF_FUNCS = ("FeatureSetByName",)

# Whether a rule may be edited by the client after it fires.
IS_EDITABLE = "EDITABLE"

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

REF_PATTERN = re.compile(
    r"(?:%s)\s*\(\s*[^,]+,\s*[\"']([^\"']+)[\"']" % "|".join(DATASET_REF_FUNCS)
)

PRO_PYTHON = (
    r"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"
)


class RuleFileError(Exception):
    """The rules file is missing, malformed, or internally inconsistent."""


# --------------------------------------------------------------------- arcpy

def _import_arcpy():
    """Import arcpy only when a real geodatabase is about to be touched.

    Kept out of module scope so --self-test runs on any Python. arcpy cannot be
    pip installed; it ships only inside ArcGIS Pro's conda environment.
    """
    try:
        import arcpy
    except ModuleNotFoundError:
        sys.exit(
            "arcpy was not found. Run this with the Python that ships with "
            "ArcGIS Pro:\n"
            '  "%s" arcade_rule_deploy.py\n'
            "or the propy.bat in ...\\Pro\\bin\\Python\\Scripts\\.\n"
            "Only --self-test runs without arcpy." % PRO_PYTHON
        )
    return arcpy


# ----------------------------------------------------------------- pure core

def extract_dataset_refs(script):
    """Dataset names a script names as string literals, in order, deduplicated.

    Only literal names can be checked ahead of time. A name built at run time
    from a variable is invisible here and is reported by callers as unchecked.
    """
    seen = []
    for name in REF_PATTERN.findall(script or ""):
        if name not in seen:
            seen.append(name)
    return seen


def has_dynamic_ref(script):
    """True when a dataset-reference call takes something other than a literal.

    Such a rule can still be deployed, but preflight cannot prove its reference
    resolves, so the caller warns instead of passing it silently.
    """
    for func in DATASET_REF_FUNCS:
        for call in re.findall(r"%s\s*\(([^)]*)" % func, script or ""):
            parts = call.split(",", 1)
            if len(parts) < 2:
                return True
            arg = parts[1].strip()
            if not (arg.startswith('"') or arg.startswith("'")):
                return True
    return False


def qualify(name, qualifier):
    """Apply a database qualifier to an unqualified dataset name.

    A name that already carries a dot is assumed qualified and is left alone,
    which is what lets one rules file target a local test geodatabase (no
    qualifier) and an enterprise one (say "GISADMIN.") without editing scripts.
    """
    if not qualifier:
        return name
    if "." in name:
        return name
    if not qualifier.endswith("."):
        qualifier += "."
    return qualifier + name


def normalize_triggers(value):
    """Accept a list or a delimited string, return an upper-case tuple."""
    if isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        parts = re.split(r"[;,]", str(value))
    out = []
    for p in parts:
        p = p.strip().upper()
        if p and p not in out:
            out.append(p)
    return tuple(out)


def load_rules(path):
    """Parse and validate a rules file. Raises RuleFileError on any problem."""
    if not os.path.isfile(path):
        raise RuleFileError("rules file not found: %s" % path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except ValueError as exc:
        raise RuleFileError("rules file is not valid JSON: %s" % exc)

    if isinstance(raw, dict):
        raw = raw.get("rules", raw)
    if not isinstance(raw, list):
        raise RuleFileError(
            "rules file must be a JSON list, or an object with a 'rules' list"
        )
    if not raw:
        raise RuleFileError("rules file contains no rules")

    rules = []
    seen = set()
    for i, item in enumerate(raw):
        where = "rule %d" % (i + 1)
        if not isinstance(item, dict):
            raise RuleFileError("%s is not an object" % where)
        for key in ("table", "name", "field", "script"):
            if not item.get(key):
                raise RuleFileError("%s is missing '%s'" % (where, key))
        triggers = normalize_triggers(item.get("triggers", "INSERT"))
        if not triggers:
            raise RuleFileError("%s has no triggering events" % where)
        bad = [t for t in triggers if t not in VALID_TRIGGERS]
        if bad:
            raise RuleFileError(
                "%s has invalid triggering events %s. Valid: %s"
                % (where, ", ".join(bad), ", ".join(VALID_TRIGGERS))
            )
        key = (item["table"].lower(), item["name"].lower())
        if key in seen:
            raise RuleFileError(
                "duplicate rule name %r on table %r. ArcGIS keys rules by name "
                "per table, so the second would overwrite the first."
                % (item["name"], item["table"])
            )
        seen.add(key)
        rules.append(
            {
                "table": item["table"],
                "name": item["name"],
                "field": item["field"],
                "script": item["script"],
                "triggers": triggers,
                "description": item.get("description", ""),
            }
        )
    return rules


def plan(rules, qualifier):
    """Resolve every rule to the paths preflight and apply will use."""
    out = []
    for r in rules:
        refs = [qualify(n, qualifier) for n in extract_dataset_refs(r["script"])]
        item = dict(r)
        item["qualified_table"] = qualify(r["table"], qualifier)
        item["refs"] = refs
        item["dynamic_refs"] = has_dynamic_ref(r["script"])
        out.append(item)
    return out


def summarize(planned):
    """Counts a caller can print or assert against without touching a database."""
    tables = []
    refs = []
    for p in planned:
        if p["qualified_table"] not in tables:
            tables.append(p["qualified_table"])
        for r in p["refs"]:
            if r not in refs:
                refs.append(r)
    return {
        "rules": len(planned),
        "tables": tables,
        "refs": refs,
        "dynamic": sum(1 for p in planned if p["dynamic_refs"]),
    }


# ------------------------------------------------------------------ geodatabase

def _ok(msg):
    print("  [ok] %s" % msg)


def _fail(msg):
    print("  [FAIL] %s" % msg)


def _warn(msg):
    print("  [warn] %s" % msg)


def preflight(planned, workspace, arcpy):
    """Check everything that can be checked before writing. True when clear."""
    ok = True

    if not arcpy.Exists(workspace):
        _fail("cannot reach workspace: %s" % workspace)
        return False
    _ok("workspace reachable: %s" % workspace)

    summary = summarize(planned)

    for ref in summary["refs"]:
        if arcpy.Exists(os.path.join(workspace, ref)):
            _ok("referenced dataset resolves: %s" % ref)
        else:
            ok = False
            _fail(
                "referenced dataset MISSING: %s. ArcGIS would accept the rule "
                "and return empty at run time." % ref
            )

    fields_by_table = {}
    for p in planned:
        fields_by_table.setdefault(p["qualified_table"], set()).add(p["field"])

    for table, fields in sorted(fields_by_table.items()):
        path = os.path.join(workspace, table)
        if not arcpy.Exists(path):
            ok = False
            _fail("target table missing: %s" % table)
            continue
        _ok("target table exists: %s" % table)
        have = set()
        has_globalid = False
        for f in arcpy.ListFields(path):
            have.add(f.name)
            have.add(f.name.upper())
            if str(f.type).lower() == "globalid":
                has_globalid = True
        # ArcGIS refuses to add any attribute rule to a table without a
        # GlobalID (ERROR 002710). Catching it here means one check run tells
        # you, instead of a batch dying part way through.
        if has_globalid:
            _ok("GlobalID present: %s" % table)
        else:
            ok = False
            _fail(
                "no GlobalID field on %s. Attribute rules require one "
                "(ERROR 002710). Add it with "
                "arcpy.management.AddGlobalIDs()." % table
            )
        for field in sorted(fields):
            if field in have or field.upper() in have:
                _ok("target field exists: %s.%s" % (table, field))
            else:
                ok = False
                _fail("target field missing: %s.%s" % (table, field))

    for p in planned:
        if p["dynamic_refs"]:
            _warn(
                "rule %s builds a dataset name at run time, so its reference "
                "could not be checked" % p["name"]
            )

    return ok


def normalize_live_triggers(events):
    """Live triggering events as an upper-case sorted tuple, or None if absent.

    Both halves of this exist because of a wrong answer someone measured.
    arcpy returns ['esriARTEInsert', 'esriARTEUpdate'] in an order that is not
    the declared order, so comparing the raw sequence reports a difference that
    is not one; and the esriARTE prefix appears in nobody's rules file.
    None is not an empty tuple. A live rule exposing no triggeringEvents is
    unknown, and calling it empty would report every rule as wrong.
    """
    if events is None:
        return None
    return tuple(sorted(str(t).upper().replace("ESRIARTE", "") for t in events))


def rule_diffs(planned, live):
    """How a live rule differs from the planned one. Empty when they agree.

    Presence is not correctness. A rule moved from INSERT to UPDATE, or bound
    to a neighbouring field, still answers to its name, so a name-only check
    calls it clean while it no longer fires on the edit it was written for.
    Field case is ignored because Describe echoes the case the field is stored
    under, e.g. St_PreDir, which is not the case the rules file declares.
    """
    diffs = []
    field = live.get("field")
    if field and field.upper() != planned["field"].upper():
        diffs.append("field expected %s, live %s" % (planned["field"], field))
    expected = tuple(sorted(planned["triggers"]))
    triggers = live.get("triggers")
    if triggers is None:
        diffs.append("triggers expected %s, live UNKNOWN (the rule exposed "
                     "none)" % ";".join(expected))
    elif triggers != expected:
        diffs.append("triggers expected %s, live %s"
                     % (";".join(expected), ";".join(triggers)))
    return diffs


def existing_rules(path, arcpy):
    """Calculation rules already on a table, keyed by upper-case name.

    The only place the live side is read, which is what keeps rule_diffs pure
    and testable without a geodatabase.
    """
    found = {}
    try:
        desc = arcpy.Describe(path)
    except Exception:
        return found
    for rule in getattr(desc, "attributeRules", []) or []:
        rtype = str(getattr(rule, "type", "")).upper()
        if RULE_TYPE in rtype or not rtype:
            found[str(rule.name).upper()] = {
                "field": getattr(rule, "fieldName", None)
                or getattr(rule, "field", None),
                "triggers": normalize_live_triggers(
                    getattr(rule, "triggeringEvents", None)),
            }
    return found


def apply_rules(planned, workspace, arcpy):
    """Add every rule, removing a same-named one first. Returns failure count."""
    added = 0
    failed = 0
    for p in planned:
        path = os.path.join(workspace, p["qualified_table"])
        try:
            arcpy.management.DeleteAttributeRule(path, p["name"], RULE_TYPE)
            print("  replaced existing %s" % p["name"])
        except Exception:
            pass
        try:
            arcpy.management.AddAttributeRule(
                in_table=path,
                name=p["name"],
                type=RULE_TYPE,
                script_expression=p["script"],
                is_editable=IS_EDITABLE,
                triggering_events=list(p["triggers"]),
                field=p["field"],
                description=p["description"] or None,
            )
            added += 1
            print(
                "  added %-20s -> %s.%s (%s)"
                % (p["name"], p["qualified_table"], p["field"],
                   ";".join(p["triggers"]))
            )
        except Exception as exc:
            failed += 1
            _fail("could not add %s: %s" % (p["name"], exc))
    print("\nADDED %d rule(s), %d failed." % (added, failed))
    return failed


def verify(planned, workspace, arcpy):
    """Confirm every planned rule is present, on its field, on its triggers.

    True when every rule matches. Checking the name alone is not enough: the
    rule that goes wrong in practice is one a colleague edited in place.
    """
    ok = True
    by_table = {}
    for p in planned:
        by_table.setdefault(p["qualified_table"], []).append(p)

    for table, items in sorted(by_table.items()):
        path = os.path.join(workspace, table)
        if not arcpy.Exists(path):
            ok = False
            _fail("table missing: %s" % table)
            continue
        present = existing_rules(path, arcpy)
        for p in items:
            live = present.get(p["name"].upper())
            if live is None:
                ok = False
                _fail("MISSING on %s: %s" % (table, p["name"]))
                continue
            diffs = rule_diffs(p, live)
            if diffs:
                ok = False
                _fail("DIFF on %s: %s - %s"
                      % (table, p["name"], "; ".join(diffs)))
            else:
                _ok("present and correct: %s.%s (field=%s triggers=%s)"
                    % (table, p["name"], live["field"],
                       ";".join(live["triggers"] or ())))
        extra = set(present) - {p["name"].upper() for p in items}
        for name in sorted(extra):
            _warn("%s carries an unmanaged calculation rule: %s" % (table, name))
    return ok


# ------------------------------------------------------------------ self-test

def self_test():
    """Assertions over the pure core. No arcpy, no geodatabase, no network."""
    import contextlib
    import io
    import tempfile

    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, fragment, label):
        try:
            fn()
        except RuleFileError as exc:
            check(fragment.lower() in str(exc).lower(),
                  "%s (message names the problem)" % label)
        except Exception as exc:
            check(False, "%s (wrong exception: %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    print("arcade_rule_deploy self-test: no arcpy, no database, no network")
    print("-" * 66)

    # ---- reference extraction, the headline check
    s1 = 'var z = FeatureSetByName($datastore, "ZIPS", ["NAME"], true);'
    check(extract_dataset_refs(s1) == ["ZIPS"], "a literal dataset name is found")
    s2 = ('var a = FeatureSetByName($datastore, "A", ["X"], true);\n'
          'var b = FeatureSetByName($datastore, "B", ["Y"], true);')
    check(extract_dataset_refs(s2) == ["A", "B"], "two references keep their order")
    s3 = ('FeatureSetByName($datastore, "DUP", ["X"], true);'
          'FeatureSetByName($datastore, "DUP", ["Y"], true);')
    check(extract_dataset_refs(s3) == ["DUP"], "a repeated reference is listed once")
    check(extract_dataset_refs("return 1;") == [],
          "a script with no reference yields none")
    check(extract_dataset_refs("") == [], "an empty script yields none")
    check(extract_dataset_refs(None) == [], "a null script yields none")
    check(extract_dataset_refs("FeatureSetByName($map,'SINGLE',['A'],true)")
          == ["SINGLE"], "single-quoted names are found")
    check(extract_dataset_refs('FeatureSetByName( $datastore , "SPACED" ,')
          == ["SPACED"], "whitespace inside the call does not hide the name")
    check(extract_dataset_refs('var t = "FeatureSetByName not a call";') == [],
          "the function name inside a string is not a reference")

    # ---- dynamic references are reported, not silently passed
    check(has_dynamic_ref('FeatureSetByName($datastore, layerName, ["A"], true)'),
          "a variable dataset name is flagged dynamic")
    check(not has_dynamic_ref('FeatureSetByName($datastore, "LIT", ["A"], true)'),
          "a literal dataset name is not flagged dynamic")
    check(not has_dynamic_ref("return $feature.X;"),
          "a script with no reference is not flagged dynamic")

    # ---- qualifier, what lets one file target test and production
    check(qualify("ROADS", "GISADMIN.") == "GISADMIN.ROADS",
          "an unqualified name takes the qualifier")
    check(qualify("ROADS", "GISADMIN") == "GISADMIN.ROADS",
          "a qualifier without a trailing dot still works")
    check(qualify("OTHER.ROADS", "GISADMIN.") == "OTHER.ROADS",
          "an already-qualified name is left alone")
    check(qualify("ROADS", "") == "ROADS", "an empty qualifier changes nothing")
    check(qualify("ROADS", None) == "ROADS", "no qualifier changes nothing")

    # ---- trigger normalization
    check(normalize_triggers("INSERT;UPDATE") == ("INSERT", "UPDATE"),
          "a semicolon-delimited string splits")
    check(normalize_triggers("insert, update") == ("INSERT", "UPDATE"),
          "commas and lower case are accepted")
    check(normalize_triggers(["Insert"]) == ("INSERT",), "a list is accepted")
    check(normalize_triggers("INSERT;INSERT") == ("INSERT",),
          "a repeated trigger is listed once")

    # ---- rules file validation
    tmp = tempfile.mkdtemp(prefix="ardtest_")

    def write(obj, name="rules.json"):
        p = os.path.join(tmp, name)
        with open(p, "w", encoding="utf-8") as fh:
            if isinstance(obj, str):
                fh.write(obj)
            else:
                json.dump(obj, fh)
        return p

    good = [{"table": "ROADS", "name": "ar_CITY", "field": "CITY",
             "triggers": "INSERT",
             "script": 'FeatureSetByName($datastore, "ZIPS", ["N"], true)'}]
    rules = load_rules(write(good))
    check(len(rules) == 1, "a valid file loads one rule")
    check(rules[0]["triggers"] == ("INSERT",), "triggers are normalized on load")

    wrapped = write({"rules": good}, "wrapped.json")
    check(len(load_rules(wrapped)) == 1, "an object with a 'rules' list loads")

    raises(lambda: load_rules(os.path.join(tmp, "nope.json")),
           "not found", "a missing file is rejected")
    raises(lambda: load_rules(write("{not json", "bad.json")),
           "valid json", "malformed JSON is rejected")
    raises(lambda: load_rules(write([], "empty.json")),
           "no rules", "an empty list is rejected")
    raises(lambda: load_rules(write({"a": 1}, "obj.json")),
           "must be a json list", "a bare object is rejected")
    raises(lambda: load_rules(write([{"name": "x", "field": "f", "script": "s"}],
                                    "notable.json")),
           "missing 'table'", "a rule without a table is rejected")
    raises(lambda: load_rules(write([{"table": "T", "field": "f", "script": "s"}],
                                    "noname.json")),
           "missing 'name'", "a rule without a name is rejected")
    raises(lambda: load_rules(write([{"table": "T", "name": "n", "script": "s"}],
                                    "nofield.json")),
           "missing 'field'", "a rule without a field is rejected")
    raises(lambda: load_rules(write([{"table": "T", "name": "n", "field": "f"}],
                                    "noscript.json")),
           "missing 'script'", "a rule without a script is rejected")
    raises(lambda: load_rules(write(
        [{"table": "T", "name": "n", "field": "f", "script": "s",
          "triggers": "SOMETIMES"}], "badtrig.json")),
        "invalid triggering", "an invalid trigger is rejected")
    raises(lambda: load_rules(write(
        [{"table": "T", "name": "dup", "field": "f", "script": "s"},
         {"table": "T", "name": "DUP", "field": "g", "script": "s"}],
        "dup.json")),
        "duplicate rule name", "two rules with one name on a table are rejected")

    two_tables = load_rules(write(
        [{"table": "A", "name": "same", "field": "f", "script": "s"},
         {"table": "B", "name": "same", "field": "f", "script": "s"}],
        "twotables.json"))
    check(len(two_tables) == 2,
          "the same rule name on two different tables is allowed")

    # ---- planning
    planned = plan(load_rules(write(good)), "GISADMIN.")
    check(planned[0]["qualified_table"] == "GISADMIN.ROADS",
          "the plan qualifies the target table")
    check(planned[0]["refs"] == ["GISADMIN.ZIPS"],
          "the plan qualifies the referenced dataset")
    check(not planned[0]["dynamic_refs"], "a literal reference is not dynamic")

    unqual = plan(load_rules(write(good)), "")
    check(unqual[0]["qualified_table"] == "ROADS",
          "no qualifier leaves the table name alone")
    check(unqual[0]["refs"] == ["ZIPS"],
          "no qualifier leaves the reference alone")

    s = summarize(plan(load_rules(write(
        [{"table": "A", "name": "r1", "field": "f",
          "script": 'FeatureSetByName($datastore, "X", ["a"], true)'},
         {"table": "A", "name": "r2", "field": "g",
          "script": 'FeatureSetByName($datastore, "X", ["b"], true)'},
         {"table": "B", "name": "r3", "field": "h",
          "script": 'FeatureSetByName($datastore, "Y", ["c"], true)'}],
        "sum.json")), ""))
    check(s["rules"] == 3, "the summary counts every rule")
    check(s["tables"] == ["A", "B"], "the summary lists each table once")
    check(s["refs"] == ["X", "Y"], "the summary lists each reference once")
    check(s["dynamic"] == 0, "the summary counts dynamic references")

    # ---- verify compares field and triggers, not only the name
    class _LiveRule(object):
        """A rule as arcpy.Describe hands it back."""

        def __init__(self, name, field, events, rtype="esriARTCalculation"):
            self.name = name
            self.type = rtype
            self.fieldName = field
            if events is not None:
                self.triggeringEvents = events

    class _Desc(object):
        def __init__(self, rules):
            self.attributeRules = rules

    class _FakeArcpy(object):
        """Just enough arcpy for verify: every path exists, one rule list."""

        def __init__(self, rules):
            self._rules = rules

        def Exists(self, path):
            return True

        def Describe(self, path):
            return _Desc(self._rules)

    # The live side is read in one place, so these pin what it reads.
    read = existing_rules("t", _FakeArcpy(
        [_LiveRule("ar_LEFTZIP", "LeftZip", ["esriARTEInsert"]),
         _LiveRule("ar_VALID", "LEFTZIP", ["esriARTEInsert"],
                   "esriARTValidation")]))
    check(sorted(read) == ["AR_LEFTZIP"],
          "a validation rule on the same table is not one of ours")
    check(read["AR_LEFTZIP"]["field"] == "LeftZip",
          "the live field is read exactly as Describe spells it")
    older = type("_OldRule", (object,), {"name": "ar_X", "field": "LEFTZIP",
                                         "type": "esriARTCalculation"})()
    check(existing_rules("t", _FakeArcpy([older]))["AR_X"]["field"] == "LEFTZIP",
          "an arcpy build that spells it 'field' is still read")

    class _Unreadable(object):
        def Describe(self, path):
            raise RuntimeError("cannot open")

    check(existing_rules("t", _Unreadable()) == {},
          "a table Describe cannot read yields no rules, never a false match"
          "  <-- pinned defect")

    check(normalize_live_triggers(["esriARTEInsert"]) == ("INSERT",),
          "a live trigger loses the esriARTE prefix nobody declares")
    check(normalize_live_triggers(["esriARTEUpdate", "esriARTEInsert"])
          == ("INSERT", "UPDATE"),
          "live triggers sort, so arcpy's order is not a difference"
          "  <-- pinned defect")
    check(normalize_live_triggers(None) is None,
          "a rule exposing no triggeringEvents is unknown, not empty"
          "  <-- pinned defect")

    decl = {"field": "LEFTZIP", "triggers": ("INSERT",)}
    check(rule_diffs(decl, {"field": "LEFTZIP", "triggers": ("INSERT",)}) == [],
          "a matching field and trigger is no difference")
    check(rule_diffs(decl, {"field": "LeftZip", "triggers": ("INSERT",)}) == [],
          "Describe echoing the stored field case is no difference"
          "  <-- pinned defect")
    check(rule_diffs(decl, {"field": None, "triggers": ("INSERT",)}) == [],
          "a rule exposing no field name is no field difference")
    d = rule_diffs(decl, {"field": "RIGHTZIP", "triggers": ("INSERT",)})
    check(len(d) == 1 and "RIGHTZIP" in d[0],
          "a rule bound to another field is a difference  <-- pinned defect")
    d = rule_diffs(decl, {"field": "LEFTZIP", "triggers": ("UPDATE",)})
    check(len(d) == 1 and "UPDATE" in d[0],
          "a rule switched from INSERT to UPDATE is a difference"
          "  <-- pinned defect")
    check(rule_diffs({"field": "F", "triggers": ("UPDATE", "INSERT")},
                     {"field": "F", "triggers": ("INSERT", "UPDATE")}) == [],
          "declared trigger order is no difference either")
    d = rule_diffs(decl, {"field": "LEFTZIP", "triggers": None})
    check(len(d) == 1 and "UNKNOWN" in d[0],
          "unreadable triggers report UNKNOWN, never equal-to-empty"
          "  <-- pinned defect")

    vplan = plan(load_rules(write(
        [{"table": "CENTERLINE", "name": "ar_LEFTZIP", "field": "LEFTZIP",
          "triggers": "INSERT", "script": "return 1;"},
         {"table": "CENTERLINE", "name": "ar_RIGHTZIP", "field": "RIGHTZIP",
          "triggers": "INSERT", "script": "return 1;"}],
        "verify.json")), "")

    clean = _FakeArcpy([_LiveRule("ar_LEFTZIP", "LeftZip", ["esriARTEInsert"]),
                        _LiveRule("ar_RIGHTZIP", "RIGHTZIP",
                                  ["esriARTEInsert"])])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        clean_ok = verify(vplan, "ws", clean)
    check(clean_ok is True,
          "two correct rules verify clean, so --verify exits 0")

    # The disaster: a colleague moves ar_LEFTZIP to UPDATE while chasing a slow
    # insert, and binds the neighbouring rule to the wrong field. Both survive
    # a name-only check.
    drifted = _FakeArcpy([_LiveRule("ar_LEFTZIP", "LEFTZIP",
                                    ["esriARTEUpdate"]),
                          _LiveRule("ar_RIGHTZIP", "LEFTZIP",
                                    ["esriARTEInsert"])])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        drift_ok = verify(vplan, "ws", drifted)
    check(drift_ok is False,
          "a rule moved to UPDATE fails verify, so --verify exits 1"
          "  <-- pinned defect")
    check(buf.getvalue().count("[FAIL] DIFF") == 2,
          "two rules with different defects both report in one run")

    # A rule deleted outright, beside one this file does not manage.
    gone = _FakeArcpy([_LiveRule("ar_RIGHTZIP", "RIGHTZIP", ["esriARTEInsert"]),
                       _LiveRule("ar_STRAY", "LEFTZIP", ["esriARTEInsert"])])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        gone_ok = verify(vplan, "ws", gone)
    check(gone_ok is False,
          "a deleted rule still fails verify  <-- pinned defect")
    check("[FAIL] MISSING" in buf.getvalue(),
          "a deleted rule reports MISSING, not DIFF")
    check("[warn]" in buf.getvalue() and "AR_STRAY" in buf.getvalue(),
          "a rule this file does not manage is a warning, not a failure")

    # Same false green one table up: no table, so no rule can be checked.
    class _NoTable(_FakeArcpy):
        def Exists(self, path):
            return False

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        no_table_ok = verify(vplan, "ws", _NoTable([]))
    check(no_table_ok is False and "table missing" in buf.getvalue(),
          "a workspace missing the table fails verify  <-- pinned defect")

    # ---- argument handling
    check(_parse(["--self-test"]).self_test, "--self-test parses")
    check(not _parse(["--rules", "r.json", "--workspace", "w"]).apply,
          "--apply defaults to off")
    check(not _parse(["--rules", "r.json", "--workspace", "w"]).verify,
          "--verify defaults to off")
    check(_parse(["--rules", "r.json", "--workspace", "w",
                  "--qualifier", "G."]).qualifier == "G.",
          "--qualifier is read")

    print("-" * 66)
    total = passed[0] + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for f in failed:
            print("  FAILED: %s" % f)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="arcade_rule_deploy.py",
        description="Deploy Arcade calculation attribute rules to a "
                    "geodatabase, preflight-checked and idempotent.",
        epilog="Config precedence: flag > environment > default. The workspace "
               "also reads $ARCADE_RULE_WORKSPACE. Nothing is written without "
               "--apply.",
    )
    ap.add_argument("--rules", help="JSON file describing the rules")
    ap.add_argument("--workspace",
                    help="target geodatabase (.gdb or .sde). "
                         "Env: ARCADE_RULE_WORKSPACE")
    ap.add_argument("--qualifier", default=os.environ.get("ARCADE_RULE_QUALIFIER", ""),
                    help="database qualifier prefixed to unqualified names, "
                         'e.g. "GISADMIN." Env: ARCADE_RULE_QUALIFIER')
    ap.add_argument("--apply", action="store_true",
                    help="add the rules. Without this nothing is written.")
    ap.add_argument("--verify", action="store_true",
                    help="report whether each rule is present, write nothing")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    if not args.rules:
        print("error: --rules is required. Use --self-test to verify the tool "
              "without a geodatabase.", file=sys.stderr)
        return 64

    workspace = args.workspace or os.environ.get("ARCADE_RULE_WORKSPACE")
    if not workspace:
        print("error: --workspace is required (or set "
              "$ARCADE_RULE_WORKSPACE).", file=sys.stderr)
        return 64

    if args.apply and args.verify:
        print("error: --apply and --verify are separate runs. Apply first, "
              "then verify.", file=sys.stderr)
        return 64

    try:
        rules = load_rules(args.rules)
    except RuleFileError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64

    planned = plan(rules, args.qualifier)
    summary = summarize(planned)
    print("%d rule(s) across %d table(s), %d referenced dataset(s)."
          % (summary["rules"], len(summary["tables"]), len(summary["refs"])))

    arcpy = _import_arcpy()

    if args.verify:
        print("\n=== VERIFY ===")
        return 0 if verify(planned, workspace, arcpy) else 1

    print("\n=== PREFLIGHT ===")
    if not preflight(planned, workspace, arcpy):
        print("\nPREFLIGHT FAILED. Nothing was written.")
        return 1
    print("\nPREFLIGHT OK.")

    if not args.apply:
        print("\nCheck only. No rule was added and nothing was written.")
        print("Re-run with --apply to add %d rule(s)." % summary["rules"])
        return 0

    print("\n=== APPLY ===")
    failed = apply_rules(planned, workspace, arcpy)
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
