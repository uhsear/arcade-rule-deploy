# arcade-rule-deploy

Deploy Arcade calculation attribute rules to a geodatabase, preflight-checked and idempotent.

Rules live in a JSON file. The default run checks and writes nothing. Adding rules needs
`--apply`, and re-running replaces rather than stacks, so the same command is safe twice.

The check that earns this tool: every `FeatureSetByName("...")` reference inside every script
is resolved against the target workspace first. ArcGIS accepts a rule whose referenced layer
does not exist. Nothing complains when you add it. The rule then returns empty at run time and
quietly writes wrong values into production.

The second check: two rules that write the same field. ArcGIS runs them one after the other, in
an evaluation order that the rules file does not show, and a re-deploy can change that order.
Preflight prints the order the rules will run in after `--apply`. It does not say which value
survives, because the rule list alone cannot tell you.

```
$ python arcade_rule_deploy.py --self-test
arcade_rule_deploy self-test: no arcpy, no database, no network
------------------------------------------------------------------
PASS  a literal dataset name is found
PASS  two references keep their order
PASS  a repeated reference is listed once
...
PASS  the function name inside a string is not a reference
PASS  a variable dataset name is flagged dynamic
...
PASS  a rule bound to another field is a difference  <-- pinned defect
PASS  a rule switched from INSERT to UPDATE is a difference  <-- pinned defect
PASS  a deleted rule still fails verify  <-- pinned defect
...
PASS  a re-deployed rule moves behind the live rule it used to precede  <-- pinned defect
...
PASS  a rule with unknown events is assumed to share them
...
PASS  the warning claims no winner
...
PASS  a unique prefix of --apply is refused, never read as --apply  <-- pinned defect
PASS  a refused flag exits 64, not 2, which means apply partly failed  <-- pinned defect
...
PASS  verify reads the live order back and it matches the prediction
...
PASS  importing the module prints nothing and exposes the core
------------------------------------------------------------------
127 assertions, 0 failed
```

## Requirements

ArcGIS Pro's Python for a real run, because attribute rules need `arcpy`:

```
"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" arcade_rule_deploy.py --self-test
```

`--self-test` needs none of that. It is pure Python 3.9 or newer and runs on any interpreter, so
you can check the tool before you have a geodatabase to point it at. Nothing to install either way.

The self-test prints the same 127 assertions on Windows (Python 3.13), on Python 3.9.25 and on
Ubuntu (Python 3.12.3), and the outputs are identical line for line.
`coverage run --branch arcade_rule_deploy.py --self-test` reports 100 percent of lines and
branches. The arcpy side runs against a geodatabase held in memory that keeps evaluation order the
way ArcGIS Pro 3.6 was measured to.

```
git clone https://github.com/uhsear/arcade-rule-deploy.git
```

## Quick start

```
python arcade_rule_deploy.py --self-test
```

## Usage

Write a rules file:

```json
[
  {
    "table": "SITES",
    "name": "ar_ZONE",
    "field": "ZONE",
    "triggers": "INSERT",
    "script": "var z = FeatureSetByName($datastore, \"ZONES\", [\"ZONE_NAME\"], true);\nvar hit = First(Intersects($feature, z));\nif (IsEmpty(hit)) { return \"NONE\"; }\nreturn hit.ZONE_NAME;"
  }
]
```

Check, then apply, then verify:

```
python arcade_rule_deploy.py --rules rules.json --workspace prod.sde
python arcade_rule_deploy.py --rules rules.json --workspace prod.sde --apply
python arcade_rule_deploy.py --rules rules.json --workspace prod.sde --verify
```

| Flag | Default | What it does |
|---|---|---|
| `--rules` | none | JSON file describing the rules. Required. |
| `--workspace` | `$ARCADE_RULE_WORKSPACE` | Target `.gdb` or `.sde`. Required. |
| `--qualifier` | `$ARCADE_RULE_QUALIFIER`, else blank | Prefix for unqualified names, e.g. `GISADMIN.` |
| `--apply` | off | Add the rules. Without it nothing is written. |
| `--verify` | off | Report whether each rule is present, on its declared field, firing on its declared triggers. Writes nothing. |
| `--self-test` | off | Run the offline assertions and exit. |

Exit codes: 0 ok, 1 preflight or verify failed, 2 apply partially failed, 64 usage error. A flag
argparse refuses, such as `--ap`, also exits 64. argparse's own code is 2, which here would read
as a partly failed apply. A same-field collision is a warning and does not change the exit code.

## Two rules on one field

A rules file deploys `ar_A`, which writes `ZONE` on insert. Later a colleague adds `ar_B` by hand,
on the same field and the same event. New rows now get `B`. Later still, somebody re-runs the
same deploy command. It is idempotent, so it deletes `ar_A` and adds it
again. New rows now get `A`. Nobody edited a rule, and nothing reported an error.

This was measured on ArcGIS Pro 3.6, on a scratch file geodatabase:

| Step | Evaluation order read back | Value on a new row |
|---|---|---|
| add `ar_A`, then `ar_B` | `ar_A` 1, `ar_B` 2 | `B` |
| delete `ar_A` | `ar_B` 1 | |
| add `ar_A` again | `ar_B` 1, `ar_A` 2 | `A` |

Esri documents the mechanism. "The evaluation order is initially determined by the order in
which rules are created for a dataset", and "the order increases by one as new rules are
created" ([Calculation attribute rules][calc]). A delete closes the gap. So a re-added rule runs
last, behind every rule it used to precede.

The re-run's preflight prints this before it writes anything. `--verify` prints the same order
afterwards, read from the live table:

```
  [warn] COLLISION on SITES.ZONE: 2 rules write this field, in this evaluation order:
  [warn]     1 of 2. ar_B on INSERT, already on the table
  [warn]     2 of 2. ar_A on INSERT, from the rules file
  [warn]     on INSERT they run ar_B, then ar_A
  [warn]     No winner is claimed. A later rule can return the value an earlier one wrote.
```

### Why it claims no winner

Running last does not mean the value is kept. In the same measurement a third rule,
`ar_KEEP`, ran last and returned `$feature.ZONE`. The new row kept `B`, the value of the rule
before it. Which value survives depends on the Arcade, and this tool does not run Arcade. So it
reports three things only: the position of each rule among all calculation rules on the table,
the events the rules share, and which rules came from the file. Rules on different events, for
example one on INSERT and one on UPDATE, never run on the same edit and are not reported.

How the order is predicted:

1. Live rules that the file does not name keep their relative order, read from
   `evaluationOrder` ([Attribute rule properties][props]).
2. The file's rules follow, in file order, because `--apply` deletes and re-adds each of them.
3. A live rule with no `evaluationOrder` makes the order UNKNOWN, and no position is printed.
4. A live rule with no readable triggering events is treated as firing on every event.
5. A batch calculation rule on the same field is reported without a position. Immediate and
   batch rules "independently maintain their own evaluation order" ([Calculation attribute
   rules][calc]), and batch rules run when rules are evaluated, not when you edit.

`--verify` reads the real order back after `--apply`, so a wrong prediction would show there.
On the run above, both printed the same order.

## What already exists

- The **Attribute Rules view** in ArcGIS Pro shows calculation rules in evaluation order, in
  separate Immediate and Batch sections, and lets you edit the Order column ([Calculation
  attribute rules][calc]). It is the best way to look at one table. It does not warn when two
  rules write the same field.
- **Reorder Attribute Rule** sets a rule's position ([Reorder Attribute Rule][reorder]). After a
  collision report it is the fix, if the order is wrong.
- **Export Attribute Rules** and **Import Attribute Rules** move rules between datasets as CSV.
  Import "will only import rules that do not already exist for the dataset, it will not update
  existing rules" ([Import Attribute Rules][import]). That is safe, but a changed rule is never
  redeployed. This tool replaces by name instead.
- [JoeGuzi/ArcGIS-Attribute-Rule-Audit](https://github.com/JoeGuzi/ArcGIS-Attribute-Rule-Audit)
  is a notebook that lists every feature class with attribute rules and their details. It audits
  what is there. It does not deploy.

## What verify checks

`--verify` reads the rules already on each table and compares three things against the rules
file: the name, the field the rule writes, and the events it fires on. A name-only check is the
one that fails you in practice, because the rule that goes wrong is one somebody edited in
place. A colleague chasing a slow insert moves `ar_LEFTZIP` from INSERT to UPDATE. The rule is
still there, still carrying the right Arcade, and it no longer fires on the insert that creates
a centerline. Any check that asks only whether the name is present reports that as healthy.

Two details are worth naming, because both were written after a wrong answer:

1. arcpy returns triggering events as `['esriARTEInsert', 'esriARTEUpdate']`, in an order that
   is not the order you declared, and the `esriARTE` prefix is in nobody's rules file. Both
   sides are stripped and sorted before comparison, so neither reports a difference that is not
   one.
2. Field names are compared without case, because `Describe` echoes the case the field is
   stored under, which is not always the case you wrote.

A rule that exposes no triggering events at all is reported as UNKNOWN, never as an empty set.
Unverifiable is not verified, so it fails the run.

`--verify` also prints the same-field collision report from the live table, with the order ArcGIS
holds. That report is a warning and does not fail the run.

## Configuration

Precedence is flag, then environment, then the default. `--workspace` falls back to
`$ARCADE_RULE_WORKSPACE` and `--qualifier` to `$ARCADE_RULE_QUALIFIER`. Nothing is written
without `--apply`, and no environment variable can turn writing on.

`--qualifier` is what lets one rules file target two databases. Names already containing a dot
are left alone, so unqualified names pick up the prefix and qualified ones do not. Point the
same file at a local test geodatabase with no qualifier, then at an enterprise one with
`--qualifier GISADMIN.`, without editing a script.

## What preflight checks

1. The workspace is reachable.
2. Every `FeatureSetByName` dataset named as a literal resolves. This is the one that matters.
3. Every target table exists.
4. Every target table carries a GlobalID. ArcGIS refuses any attribute rule without one and
   fails with ERROR 002710, so one check run tells you rather than a batch dying part way.
5. Every target field exists on its table.
6. Rule names are unique per table, triggers are valid, and no required key is missing.
7. Two rules that write the same field on the same event get a warning. The warning gives the
   evaluation order after `--apply` and names no winner. See "Two rules on one field".

A script that builds a dataset name at run time is reported as unchecked rather than passed
silently, because there is nothing to resolve ahead of time.

## Why the obvious version is wrong

`AddAttributeRule` validates the Arcade syntax, not the world the script runs in. A rule
referencing `ZIPCODES` when the table is really `GISADMIN.ZIPCODES` is added without complaint.
`FeatureSetByName` then returns an empty set, `First()` returns null, and a rule written to fall
back to the existing value silently keeps stale data while looking like it ran.

Applying rules one at a time by hand has the same problem in a different place: a batch that
dies on rule 7 of 12 leaves the table half configured, and the obvious retry adds duplicates of
the first six. Deleting a same-named rule before adding it makes the whole run repeatable.

## Limits

- Calculation rules only. Constraint and validation rules take different parameters and are out
  of scope.
- Only dataset names written as string literals can be resolved. A name built from a variable is
  flagged, not checked.
- Only `FeatureSetByName` is inspected. `FeatureSetByPortalItem` and friends are not.
- No removal mode. Deleting rules you no longer want is `arcpy.management.DeleteAttributeRule`.
- The Arcade itself is never executed here. Preflight proves the rule can be added and its
  references exist. Whether the expression returns the right value is your test to write.
- Enterprise geodatabases need the schema lock, so nothing else can be editing the table.
- `--verify` compares the name, field and triggers. It does not compare the Arcade text, so a
  rule whose expression was rewritten in place still verifies clean.
- The collision report matches rules by their target field only. A calculation rule can return a
  dictionary that "edits specified fields", and with that form "the target field in the attribute
  rule is optional" ([Attribute rule dictionary keywords][dict]). A write made that way is not seen.
- The collision report does not read `isEnabled`, `subtypeCode` or `triggeringFields`
  ([Attribute rule properties][props]). A disabled rule, rules on different subtypes, and an
  update rule limited to other fields are all still reported. Each can be switched back on or
  edited, so the report errs toward naming them.
- The evaluation order was measured on ArcGIS Pro 3.6 against a file geodatabase only. The
  enterprise geodatabase and the batch rule handling follow Esri's documentation and were not
  measured.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [gdbxray](https://github.com/uhsear/gdbxray) - read the attribute rules already in a geodatabase, with their Arcade text
- [arcadecheck](https://github.com/uhsear/arcadecheck) - inventory the expressions before you migrate them

[calc]: https://pro.arcgis.com/en/pro-app/3.4/help/data/geodatabases/overview/calculation-attribute-rules.htm
[props]: https://pro.arcgis.com/en/pro-app/3.4/arcpy/functions/attribute-rule-properties.htm
[reorder]: https://pro.arcgis.com/en/pro-app/3.4/tool-reference/data-management/reorder-attribute-rule.htm
[import]: https://pro.arcgis.com/en/pro-app/3.4/tool-reference/data-management/import-attribute-rules.htm
[dict]: https://pro.arcgis.com/en/pro-app/3.4/help/data/geodatabases/overview/attribute-rule-dictionary-keywords.htm
