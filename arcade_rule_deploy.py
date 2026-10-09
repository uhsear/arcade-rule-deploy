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
        # A collision is a warning, not a failure: two rules on one field
        # can be deliberate. What must not happen is not knowing.
        rules, known = run_order(
            [p for p in planned if p["qualified_table"] == table],
            existing_rules(path, arcpy))
        for line in collision_lines(table, rules, known):
            _warn(line)

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
                "name": str(rule.name),
                "field": getattr(rule, "fieldName", None)
                or getattr(rule, "field", None),
                "triggers": normalize_live_triggers(
                    getattr(rule, "triggeringEvents", None)),
                "order": getattr(rule, "evaluationOrder", None),
                "batch": bool(getattr(rule, "batch", False)),
            }
    return found


def run_order(planned, live):
    """Calculation rules on one table in the order they run after --apply.

    Measured on Pro 3.6: a new rule takes the next evaluation order, and a
    delete closes the gap. --apply deletes and re-adds every rule in the file,
    in file order, so live rules the file does not name keep their relative
    places at the front and the file's rules follow. A live rule the file names
    is dropped here, because apply replaces it. Batch rules keep a separate
    order and run when rules are evaluated, so they go last, unnumbered.

    Returns (rules, known). known is False when a live immediate rule exposed
    no evaluationOrder, because then no position can be stated.
    """
    names = set(p["name"].upper() for p in planned)
    kept = [dict(v, source="live") for k, v in sorted(live.items())
            if k not in names]
    immediate = [r for r in kept if not r["batch"]]
    known = all(r["order"] is not None for r in immediate)
    immediate.sort(key=lambda r: r["order"] or 0)
    ours = [{"name": p["name"], "field": p["field"], "triggers":
             tuple(p["triggers"]), "batch": False, "source": "file"}
            for p in planned]
    return immediate + ours + [r for r in kept if r["batch"]], known


def field_collisions(rules):
    """Fields that two or more rules write on the same edit, in run order.

    Returns a list of (field, rules on that field, {event: [rule names]}).
    Only events two immediate rules share are listed, because rules on
    different events never run on the same edit. A rule whose events are
    unknown counts on every event: it cannot be ruled out. A batch rule
    collides with any other rule on its field, since it rewrites the field
    whenever rules are evaluated. Deliberately no winner is named: the last
    rule to run can return the value an earlier one wrote, and this reads the
    rule list, not the Arcade.
    """
    order = []
    by_field = {}
    for r in rules:
        if not r["field"]:
            continue
        key = r["field"].upper()
        if key not in by_field:
            by_field[key] = []
            order.append(key)
        by_field[key].append(r)
    out = []
    for key in order:
        group = by_field[key]
        shared = {}
        for event in VALID_TRIGGERS:
            names = [r["name"] for r in group if not r["batch"]
                     and (r["triggers"] is None or event in r["triggers"])]
            if len(names) > 1:
                shared[event] = names
        if shared or (len(group) > 1 and any(r["batch"] for r in group)):
            out.append((group[0]["field"], group, shared))
    return out


def collision_lines(table, rules, known):
    """Warning text for every same-field collision on one table.

    States the run position of each rule and the events they share, and says
    in words that no value is predicted.
    """
    lines = []
    immediate = [r for r in rules if not r["batch"]]
    for field, group, shared in field_collisions(rules):
        lines.append("COLLISION on %s.%s: %d rules write this field, in this "
                     "evaluation order%s:"
                     % (table, field, len(group),
                        "" if known else " (UNKNOWN: a live rule exposed no "
                        "evaluationOrder)"))
        for r in group:
            if r["batch"]:
                pos = "batch"
            elif known:
                pos = "%d of %d" % (immediate.index(r) + 1, len(immediate))
            else:
                pos = "?"
            lines.append("    %s. %s on %s, %s"
                         % (pos, r["name"],
                            ";".join(r["triggers"]) if r["triggers"]
                            is not None else "UNKNOWN events",
                            "from the rules file" if r["source"] == "file"
                            else "already on the table"))
        for event in VALID_TRIGGERS:
            if event in shared:
                lines.append("    on %s they run %s"
                             % (event, ", then ".join(shared[event])))
        lines.append("    No winner is claimed. A later rule can return the "
                     "value an earlier one wrote.")
    return lines


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
        # The live order, read back, is what proves the preflight prediction.
        rules, known = run_order([], present)
        for line in collision_lines(table, rules, known):
            _warn(line)
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

    check(existing_rules("t", _FakeArcpy([_LiveRule("ar_N", "F", None)]))
          ["AR_N"]["triggers"] is None,
          "a live rule exposing no triggeringEvents is read as unknown")
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

    # ---- same-field collisions: report the order, claim no winner
    def live(name, field, triggers, order, batch=False):
        return {"name": name, "field": field, "triggers": triggers,
                "order": order, "batch": batch}

    def ours(name, field, triggers="INSERT"):
        return {"name": name, "field": field,
                "triggers": normalize_triggers(triggers)}

    def names(rules):
        return [r["name"] for r in rules]

    got, known = run_order([ours("ar_A", "ZONE"), ours("ar_B", "ZONE")], {})
    check(names(got) == ["ar_A", "ar_B"] and known,
          "the file's rules run in file order, because apply adds them so")
    # Measured on Pro 3.6: delete ar_A, add it again, and it runs after a
    # rule it used to precede. Re-running the same deploy flipped the value.
    got, known = run_order([ours("ar_A", "ZONE")],
                           {"AR_A": live("ar_A", "ZONE", ("INSERT",), 1),
                            "AR_KEEP": live("ar_KEEP", "ZONE", ("INSERT",), 2)})
    check(names(got) == ["ar_KEEP", "ar_A"],
          "a re-deployed rule moves behind the live rule it used to precede"
          "  <-- pinned defect")
    check(got[0]["source"] == "live" and got[1]["source"] == "file",
          "each rule says whether it is live or from the rules file")
    got, known = run_order([], {"AR_A": live("ar_A", "Z", ("INSERT",), 2),
                                "AR_Z": live("ar_Z", "Z", ("INSERT",), 1)})
    check(names(got) == ["ar_Z", "ar_A"],
          "live rules follow evaluationOrder, not their names")
    got, known = run_order([], {"AR_A": live("ar_A", "Z", ("INSERT",), 1),
                                "AR_B": live("ar_B", "Z", ("INSERT",), None)})
    check(known is False,
          "a live rule with no evaluationOrder makes the order unknown")
    got, known = run_order([ours("ar_F", "Z")],
                           {"AR_BAT": live("ar_BAT", "Z", ("INSERT",), 1,
                                           True)})
    check(names(got) == ["ar_F", "ar_BAT"] and known,
          "a batch rule goes last and does not spoil the immediate order")

    def rule(name, field, triggers, batch=False, source="file"):
        return {"name": name, "field": field, "triggers": triggers,
                "batch": batch, "source": source}

    c = field_collisions([rule("ar_A", "ZONE", ("INSERT",)),
                          rule("ar_B", "ZONE", ("INSERT", "UPDATE"))])
    check(len(c) == 1 and c[0][2] == {"INSERT": ["ar_A", "ar_B"]},
          "two rules on one field and one event collide on that event only")
    check(field_collisions([rule("ar_A", "ZONE", ("INSERT",)),
                            rule("ar_B", "ZONE", ("UPDATE",))]) == [],
          "rules on one field but different events never collide")
    check(field_collisions([rule("ar_A", "ZONE", ("INSERT",)),
                            rule("ar_B", "CITY", ("INSERT",))]) == [],
          "rules on different fields never collide")
    check(len(field_collisions([rule("ar_A", "Zone", ("INSERT",)),
                                rule("ar_B", "ZONE", ("INSERT",))])) == 1,
          "field case does not hide a collision")
    c = field_collisions([rule("ar_A", "ZONE", ("INSERT",)),
                          rule("ar_B", "ZONE", None, source="live")])
    check(len(c) == 1 and c[0][2]["INSERT"] == ["ar_A", "ar_B"],
          "a rule with unknown events is assumed to share them")
    c = field_collisions([rule("ar_A", "ZONE", ("INSERT",)),
                          rule("ar_B", "ZONE", ("UPDATE",)),
                          rule("ar_C", "ZONE", ("INSERT", "UPDATE"))])
    check(len(c) == 1 and c[0][2] == {"INSERT": ["ar_A", "ar_C"],
                                      "UPDATE": ["ar_B", "ar_C"]},
          "three rules list each shared event with its own run order")
    check(len(field_collisions([rule("ar_A", "ZONE", ("INSERT",)),
                                rule("ar_BAT", "ZONE", ("INSERT",), True,
                                     "live")])) == 1,
          "a batch rule on the same field is a collision")
    check(field_collisions([rule("ar_BAT", "ZONE", ("INSERT",), True)]) == [],
          "a batch rule alone on its field is not a collision")
    check(field_collisions([rule("ar_D", None, ("INSERT",)),
                            rule("ar_E", None, ("INSERT",))]) == [],
          "rules exposing no field are not matched to each other")

    text = "\n".join(collision_lines("SITES", [
        rule("ar_LEGACY", "ZONE", ("INSERT",), source="live"),
        rule("ar_CITY", "CITY", ("INSERT",)),
        rule("ar_ZONE", "Zone", ("INSERT", "UPDATE"))], True))
    check("COLLISION on SITES.ZONE: 2 rules" in text,
          "the warning names the table and the field")
    check("1 of 3. ar_LEGACY" in text and "3 of 3. ar_ZONE" in text,
          "the warning gives each rule its position among all rules on the "
          "table")
    check("already on the table" in text and "from the rules file" in text,
          "the warning says which rule is live and which is being deployed")
    check("on INSERT they run ar_LEGACY, then ar_ZONE" in text,
          "the warning states the order on the shared event")
    check("No winner is claimed" in text and "wins" not in text
          and "kept" not in text,
          "the warning claims no winner")
    text = "\n".join(collision_lines("T", [
        rule("ar_A", "ZONE", None, source="live"),
        rule("ar_B", "ZONE", ("INSERT",)),
        rule("ar_BAT", "ZONE", ("UPDATE",), True, "live")], False))
    check("UNKNOWN: a live rule exposed no evaluationOrder" in text
          and "?. ar_B" in text,
          "an unknown order prints no position rather than a guessed one")
    check("UNKNOWN events" in text and "batch. ar_BAT" in text,
          "unknown events and batch rules are labelled, not numbered")
    check(collision_lines("T", [rule("ar_A", "ZONE", ("INSERT",))], True)
          == [], "one rule per field prints nothing")

    # ---- argument handling
    check(_parse(["--self-test"]).self_test, "--self-test parses")
    check(not _parse(["--rules", "r.json", "--workspace", "w"]).apply,
          "--apply defaults to off")
    check(not _parse(["--rules", "r.json", "--workspace", "w"]).verify,
          "--verify defaults to off")
    # A unique prefix of --apply must not be read as --apply, or a typo writes.
    refused = False
    usage_code = None
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            _parse(["--rules", "r.json", "--workspace", "w", "--ap"])
    except SystemExit as exc:
        refused = True
        usage_code = exc.code
    check(refused and not _parse(["--rules", "r.json", "--workspace", "w"]).apply,
          "a unique prefix of --apply is refused, never read as --apply  <-- pinned defect")
    # argparse exits 2 on a usage error, and 2 here means apply partly failed.
    check(usage_code == 64,
          "a refused flag exits 64, not 2, which means apply partly failed"
          "  <-- pinned defect")
    check(_parse(["--rules", "r.json", "--workspace", "w",
                  "--qualifier", "G."]).qualifier == "G.",
          "--qualifier is read")

    # ---- the arcpy side, against a geodatabase held in memory. It keeps the
    # evaluation order the way Pro 3.6 was measured to: a new rule goes last,
    # a delete closes the gap.
    class _Field(object):
        def __init__(self, name, ftype="String"):
            self.name = name
            self.type = ftype

    class _Management(object):
        def __init__(self, gdb):
            self.gdb = gdb
            self.writes = 0

        def DeleteAttributeRule(self, path, name, rtype):
            rules = self.gdb.rules.get(os.path.basename(path), [])
            for r in rules:
                if r.name == name:
                    self.writes += 1
                    rules.remove(r)
                    return
            raise RuntimeError("rule %s does not exist" % name)

        def AddAttributeRule(self, in_table, name, type, script_expression,
                             is_editable, triggering_events, field,
                             description):
            if "BROKEN" in script_expression:
                raise RuntimeError("the Arcade did not compile")
            self.writes += 1
            self.gdb.rules.setdefault(os.path.basename(in_table), []).append(
                _LiveRule(name, field, ["esriARTE" + t.capitalize()
                                        for t in triggering_events]))

    class _Gdb(object):
        def __init__(self, tables, refs=(), reachable=True):
            self.tables = tables
            self.refs = set(refs)
            self.reachable = reachable
            self.rules = {}
            self.management = _Management(self)

        def Exists(self, path):
            name = os.path.basename(path)
            if name == "ws":
                return self.reachable
            return name in self.tables or name in self.refs

        def ListFields(self, path):
            return self.tables[os.path.basename(path)]

        def Describe(self, path):
            rules = self.rules.get(os.path.basename(path), [])
            for i, r in enumerate(rules):
                r.evaluationOrder = i + 1
            return _Desc(rules)

    def sites():
        return _Gdb({"SITES": [_Field("GlobalID", "GlobalID"),
                               _Field("Zone")]}, ["ZONES"])

    zone_rules = [
        {"table": "SITES", "name": "ar_ZONE", "field": "ZONE",
         "triggers": "INSERT;UPDATE",
         "script": 'FeatureSetByName($datastore, "ZONES", ["N"], true)'},
        {"table": "SITES", "name": "ar_ZONE_FIX", "field": "ZONE",
         "triggers": "INSERT", "script": 'return "FIXED";'}]
    zplan = plan(load_rules(write(zone_rules, "zone.json")), "")

    def run(fn, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = fn(*args)
        return result, out.getvalue()

    gdb = sites()
    gdb.rules["SITES"] = [_LiveRule("ar_ZONE", "ZONE", ["esriARTEInsert"]),
                          _LiveRule("ar_LEGACY", "ZONE", ["esriARTEInsert"])]
    ok, out = run(preflight, zplan, "ws", gdb)
    check(ok is True and "[FAIL]" not in out,
          "a clean workspace passes preflight with a collision on it")
    check("[warn] COLLISION on SITES.ZONE: 3 rules" in out,
          "preflight warns about the collision, live rule included")
    check("1 of 3. ar_LEGACY" in out and "2 of 3. ar_ZONE on" in out
          and "3 of 3. ar_ZONE_FIX" in out,
          "preflight predicts the order apply will leave behind")
    check(gdb.management.writes == 0,
          "preflight writes nothing, collision or not")
    failed_apply, out = run(apply_rules, zplan, "ws", gdb)
    order = [(r.name, r.evaluationOrder)
             for r in gdb.Describe("ws/SITES").attributeRules]
    check(failed_apply == 0 and order == [("ar_LEGACY", 1), ("ar_ZONE", 2),
                                          ("ar_ZONE_FIX", 3)],
          "apply replaces the live rule and leaves the predicted order")
    check("replaced existing ar_ZONE" in out and "ADDED 2 rule(s), 0 failed"
          in out, "apply reports the replacement and the count")
    ok, out = run(verify, zplan, "ws", gdb)
    check(ok is True and "1 of 3. ar_LEGACY" in out
          and "3 of 3. ar_ZONE_FIX" in out,
          "verify reads the live order back and it matches the prediction")
    check("on INSERT they run ar_LEGACY, then ar_ZONE, then ar_ZONE_FIX" in out
          and "unmanaged calculation rule: AR_LEGACY" in out,
          "verify reports the collision and the unmanaged rule, and passes")

    broken = plan(load_rules(write(
        [{"table": "SITES", "name": "ar_BAD", "field": "ZONE",
          "script": "BROKEN"}], "broken.json")), "")
    failed_apply, out = run(apply_rules, broken, "ws", sites())
    check(failed_apply == 1 and "[FAIL] could not add ar_BAD" in out,
          "a rule ArcGIS refuses is counted as failed, not added")

    ok, out = run(preflight, zplan, "ws", _Gdb({}, reachable=False))
    check(ok is False and "cannot reach workspace" in out,
          "an unreachable workspace fails preflight before anything else")
    bad = plan(load_rules(write(
        [{"table": "SITES", "name": "ar_X", "field": "NOFIELD",
          "script": 'FeatureSetByName($datastore, "NOPE", ["N"], true)'},
         {"table": "GHOST", "name": "ar_Y", "field": "F",
          "script": 'FeatureSetByName($datastore, name, ["N"], true)'},
         {"table": "BARE", "name": "ar_Z", "field": "F", "script": "1"}],
        "bad.json")), "")
    ok, out = run(preflight, bad, "ws",
                  _Gdb({"SITES": [_Field("GlobalID", "GlobalID")],
                        "BARE": [_Field("F")]}))
    check(ok is False and "referenced dataset MISSING: NOPE" in out,
          "a missing referenced dataset fails preflight")
    check("target table missing: GHOST" in out,
          "a missing target table fails preflight")
    check("no GlobalID field on BARE" in out,
          "a table without a GlobalID fails preflight")
    check("target field missing: SITES.NOFIELD" in out,
          "a missing target field fails preflight")
    check("ar_Y builds a dataset name at run time" in out,
          "a dynamic reference is a warning in preflight")
    check(has_dynamic_ref("FeatureSetByName($datastore)"),
          "a call with no dataset argument is flagged dynamic")
    raises(lambda: load_rules(write(["not an object"], "str.json")),
           "not an object", "a rule that is not an object is rejected")
    raises(lambda: load_rules(write(
        [{"table": "T", "name": "n", "field": "f", "script": "s",
          "triggers": " ; "}], "notrig.json")),
        "no triggering events", "a rule with no triggering events is rejected")

    # ---- arcpy import and the command line, end to end
    env_keys = ("ARCADE_RULE_WORKSPACE", "ARCADE_RULE_QUALIFIER")
    saved_mod = dict((k, v) for k, v in sys.modules.items() if k == "arcpy")
    saved_env = dict((k, v) for k, v in os.environ.items() if k in env_keys)

    def set_env(workspace):
        for k in env_keys:
            os.environ.pop(k, None)
        if workspace:
            os.environ["ARCADE_RULE_WORKSPACE"] = workspace

    def cli(argv, arcpy=None):
        sys.modules["arcpy"] = arcpy
        err = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue() + err.getvalue()

    zfile = os.path.join(tmp, "zone.json")
    try:
        set_env(None)
        code, out = cli(["--rules", zfile, "--workspace", "ws"], None)
        check("arcpy was not found" in str(code),
              "without arcpy a real run stops and names the Pro Python")
        code, out = cli(["--workspace", "ws"])
        check(code == 64 and "--rules is required" in out,
              "no --rules is a usage error, exit 64")
        code, out = cli(["--rules", zfile])
        check(code == 64 and "--workspace is required" in out,
              "no workspace and no environment is a usage error, exit 64")
        code, out = cli(["--rules", zfile, "--workspace", "ws", "--apply",
                         "--verify"])
        check(code == 64 and "separate runs" in out,
              "--apply with --verify is refused, exit 64")
        code, out = cli(["--rules", os.path.join(tmp, "dup.json"),
                         "--workspace", "ws"])
        check(code == 64 and "duplicate rule name" in out,
              "a bad rules file is a usage error, exit 64")
        gdb = sites()
        code, out = cli(["--rules", zfile, "--workspace", "ws"], gdb)
        check(code == 0 and "Check only" in out and "COLLISION" in out
              and gdb.management.writes == 0,
              "a check run warns, exits 0 and writes nothing")
        set_env("ws")
        code, out = cli(["--rules", zfile, "--apply"], gdb)
        check(code == 0 and gdb.management.writes == 2,
              "--apply writes, and the workspace comes from the environment")
        code, out = cli(["--rules", zfile, "--verify"], gdb)
        check(code == 0 and "present and correct" in out,
              "--verify after --apply exits 0")
        code, out = cli(["--rules", zfile, "--verify"], sites())
        check(code == 1, "--verify with the rules absent exits 1")
        code, out = cli(["--rules", os.path.join(tmp, "bad.json")],
                        sites())
        check(code == 1 and "Nothing was written" in out,
              "a failed preflight exits 1 and writes nothing")
        code, out = cli(["--rules", os.path.join(tmp, "broken.json"),
                         "--apply"], sites())
        check(code == 2, "a partly failed apply exits 2")
    finally:
        sys.modules.pop("arcpy", None)
        sys.modules.update(saved_mod)
        set_env(None)
        os.environ.update(saved_env)

    # ---- the harness itself. A check() that cannot record a failure would
    # report every defect above as a pass. The probe's output is swallowed.
    mark = len(failed)
    with contextlib.redirect_stdout(io.StringIO()):
        check(False, "probe: a false condition is a failure")
        raises(lambda: None, "x", "probe: nothing raised is a failure")
        raises(lambda: [][0], "x", "probe: the wrong exception is a failure")
    probe = failed[mark:]
    del failed[mark:]
    check(len(probe) == 3,
          "check() and raises() really do record a failure")
    red, out = run(_tally, 1, ["probe one", "probe two"])
    check(red == 1 and "3 assertions, 2 failed" in out
          and "  FAILED: probe two" in out,
          "the footer reports failures by count and by name, and exits 1")

    # Importing the module runs nothing, so another script may use the core.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "arcade_rule_deploy_imported", os.path.abspath(__file__))
    imported = importlib.util.module_from_spec(spec)
    cache_before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        _, out = run(spec.loader.exec_module, imported)
    finally:
        sys.dont_write_bytecode = cache_before
    check(out == "" and imported.qualify("A", "G") == "G.A",
          "importing the module prints nothing and exposes the core")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return _tally(passed[0], failed)


def _tally(passed, failed):
    """The self-test footer. Returns the exit code."""
    print("-" * 66)
    total = passed + len(failed)
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
        allow_abbrev=False,
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

    def usage_error(message):
        ap.print_usage(sys.stderr)
        ap.exit(64, "%s: error: %s\n" % (ap.prog, message))

    ap.error = usage_error
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
