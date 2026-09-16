# arcade-rule-deploy

Deploy Arcade calculation attribute rules to a geodatabase, preflight-checked and idempotent.

Rules live in a JSON file. The default run checks and writes nothing. Adding rules needs
`--apply`, and re-running replaces rather than stacks, so the same command is safe twice.

The check that earns this tool: every `FeatureSetByName("...")` reference inside every script
is resolved against the target workspace first. ArcGIS accepts a rule whose referenced layer
does not exist. Nothing complains when you add it. The rule then returns empty at run time and
quietly writes wrong values into production.

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
------------------------------------------------------------------
48 assertions, 0 failed
```

## Requirements

ArcGIS Pro's Python for a real run, because attribute rules need `arcpy`:

```
"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" arcade_rule_deploy.py --self-test
```

`--self-test` needs none of that. It is pure Python 3.8+ and runs on any interpreter, so you can
check the tool before you have a geodatabase to point it at. Nothing to install either way.

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
| `--verify` | off | Report whether each rule is present. Writes nothing. |
| `--self-test` | off | Run the offline assertions and exit. |

Exit codes: 0 ok, 1 preflight or verify failed, 2 apply partially failed, 64 usage error.

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

## Limitations

- Calculation rules only. Constraint and validation rules take different parameters and are out
  of scope.
- Only dataset names written as string literals can be resolved. A name built from a variable is
  flagged, not checked.
- Only `FeatureSetByName` is inspected. `FeatureSetByPortalItem` and friends are not.
- No removal mode. Deleting rules you no longer want is `arcpy.management.DeleteAttributeRule`.
- The Arcade itself is never executed here. Preflight proves the rule can be added and its
  references exist. Whether the expression returns the right value is your test to write.
- Enterprise geodatabases need the schema lock, so nothing else can be editing the table.

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
