#!/usr/bin/env python3
"""Breaking changes in the R1 contract between the previous release and this checkout (PR-V-4, QA-2).

Compares every `docs/openapi/<module>.json` at the newest `smo-v*` tag (or `--base REF`) with the working tree and fails on a change a client
written against the previous release could trip over:

  removed-module           a module whose spec file is gone (the whole module was removed)
  removed-operation        a path + method that is gone
  new-required-parameter   a parameter that did not exist, or was optional, and is required now
  narrowed-parameter       a parameter (or request property) whose enum lost a value, or whose maximum / maxLength / maxItems went down, or whose
                           minimum / minLength went up
  changed-type             a parameter, request property or response property whose type changed
  new-required-property    a request body property that is required now and was not
  removed-property         a response property that a client could read (listed in `required` before) and is gone
  removed-response         a 2xx response that is no longer documented

Deliberate breaks go in `scripts/breaking_change_waivers.json` as `"<module> <rule> <METHOD> <path> <where>": "reason"`
(`where` is the parameter or property, e.g. `query offset`; a key may use shell wildcards, `*`, to waive one reasoned change across routes). The keys name the
comparison with ONE release, so the file is emptied when a release is cut (a waiver that matches nothing fails, which is how it gets noticed).
Adding an operation, an optional property, an enum value to a response or a documented error status is never a break.

  scripts/check_breaking_changes.py [--base REF] [--list]

Exit status: 0 no break, 1 an unwaived break or a stale waiver, and a non-zero exit with an `error:` message when the base cannot be used: no release tag, a `--base` that does not
resolve to a commit, or a base that has no OpenAPI documents. A comparison that could not be made never passes.
"""

import argparse
import fnmatch
import json
import subprocess
import sys
from pathlib import Path

SMO = Path(__file__).resolve().parent.parent
REPO = SMO.parent
METHODS = ("get", "put", "post", "delete", "patch", "head", "options")
WAIVERS_FILE = Path(__file__).with_name("breaking_change_waivers.json")


def resolve(spec: dict, node, depth: int = 0):
    """Follows `$ref`s (components only); a cycle stops at the depth limit."""
    while isinstance(node, dict) and "$ref" in node and depth < 12:
        target = spec
        for part in node["$ref"].removeprefix("#/").split("/"):
            target = target.get(part, {})
        node, depth = target, depth + 1
    return node if isinstance(node, dict) else {}


def type_of(spec: dict, schema) -> str:
    """A comparable type name for a schema: its `type`, or the sorted `|`-joined types of its `anyOf`/`oneOf`, or `enum`, or `any` when nothing is said (and `any` never counts as a change)."""
    schema = resolve(spec, schema)
    if "type" in schema:
        return str(schema["type"])
    options = schema.get("anyOf") or schema.get("oneOf")
    if options:
        return "|".join(sorted({type_of(spec, o) for o in options}))
    if "enum" in schema:
        return "enum"
    return "any"


def bounds_change(old: dict, new: dict) -> list[str]:
    """The ways `new` is stricter than `old` for what a client sends: a lower maximum, a higher minimum, a bound that was not there before, an enum that lost a value, or an enum that was added. Returns one text per change."""
    out = []
    for key in ("maximum", "maxLength", "maxItems", "exclusiveMaximum"):
        if key in new and (key not in old or new[key] < old[key]):
            out.append(f"{key} {old.get(key, 'unbounded')} -> {new[key]}")
    for key in ("minimum", "minLength", "minItems", "exclusiveMinimum"):
        if key in new and (key not in old or new[key] > old[key]):
            out.append(f"{key} {old.get(key, 'unbounded')} -> {new[key]}")
    if "enum" in old and "enum" in new:
        gone = [v for v in old["enum"] if v not in new["enum"]]
        if gone:
            out.append(f"enum lost {gone}")
    elif "enum" in new and "enum" not in old:
        out.append("enum added")
    return out


def compare_schema(old_spec, old, new_spec, new, where: str, request: bool, findings: list, depth: int = 0) -> None:
    """request=True: what a client sends (must not narrow); False: what it reads (must not lose a property it could rely on)."""
    old, new = resolve(old_spec, old), resolve(new_spec, new)
    if depth > 6 or not old or not new:
        return
    old_type, new_type = type_of(old_spec, old), type_of(new_spec, new)
    if old_type != new_type and "any" not in (old_type, new_type):
        # a response that now may also be null, or a request that now also accepts more, is not a break
        old_set, new_set = set(old_type.split("|")), set(new_type.split("|"))
        widened = old_set <= new_set if request else new_set <= old_set
        if not widened:
            findings.append(("changed-type", where, f"{old_type} -> {new_type}"))
            return
    if request:
        for change in bounds_change(old, new):
            findings.append(("narrowed-parameter", where, change))
    old_props, new_props = old.get("properties", {}), new.get("properties", {})
    old_required, new_required = set(old.get("required", [])), set(new.get("required", []))
    if request:
        for name in sorted(new_required - old_required):
            findings.append(("new-required-property", f"{where}.{name}", "required now"))
    else:
        for name in sorted(old_required):
            if name not in new_props:
                findings.append(("removed-property", f"{where}.{name}", "a required property of the response is gone"))
    for name, old_prop in old_props.items():
        if name in new_props:
            compare_schema(old_spec, old_prop, new_spec, new_props[name], f"{where}.{name}", request, findings, depth + 1)
    if "items" in old and "items" in new:
        compare_schema(old_spec, old["items"], new_spec, new["items"], f"{where}[]", request, findings, depth + 1)


def parameters(spec: dict, path_item: dict, operation: dict) -> dict:
    """The operation's parameters, with the path-level ones included, as `{(location, name): parameter}`; `$ref`s are resolved. An operation-level parameter replaces a path-level one of the same key."""
    out = {}
    for p in list(path_item.get("parameters", [])) + list(operation.get("parameters", [])):
        p = resolve(spec, p)
        out[(p.get("in"), p.get("name"))] = p
    return out


def body_schema(spec: dict, operation: dict):
    """`(request body object, its JSON schema)` of an operation; the schema comes from `application/json`, else from the first media type listed, else None."""
    body = resolve(spec, operation.get("requestBody", {}))
    content = body.get("content", {})
    media = content.get("application/json") or next(iter(content.values()), {})
    return body, media.get("schema")


def compare_specs(module: str, old: dict, new: dict) -> list[tuple[str, str, str, str]]:
    """Returns (rule, "METHOD path", where, detail) for each break."""
    findings = []
    for path, old_item in old.get("paths", {}).items():
        new_item = new.get("paths", {}).get(path)
        for method in METHODS:
            if method not in old_item:
                continue
            op_id = f"{method.upper()} {path}"
            if new_item is None or method not in new_item:
                findings.append(("removed-operation", op_id, "", "the operation is gone"))
                continue
            old_op, new_op = old_item[method], new_item[method]
            local: list = []
            old_params, new_params = parameters(old, old_item, old_op), parameters(new, new_item, new_op)
            for key, param in new_params.items():
                before = old_params.get(key)
                if param.get("required") and (before is None or not before.get("required")):
                    local.append(("new-required-parameter", f"{key[0]} {key[1]}", "required now" if before else "new and required"))
                if before is not None:
                    compare_schema(old, before.get("schema", {}), new, param.get("schema", {}), f"{key[0]} {key[1]}", True, local)
            old_body, old_schema = body_schema(old, old_op)
            new_body, new_schema = body_schema(new, new_op)
            if new_body.get("required") and not old_body.get("required"):
                local.append(("new-required-parameter", "body", "the request body is required now"))
            if old_schema and new_schema:
                compare_schema(old, old_schema, new, new_schema, "body", True, local)
            for status, old_resp in old_op.get("responses", {}).items():
                if not status.startswith("2"):
                    continue
                new_resp = new_op.get("responses", {}).get(status)
                if new_resp is None:
                    local.append(("removed-response", status, "the success response is no longer documented"))
                    continue
                old_media = resolve(old, old_resp).get("content", {}).get("application/json", {})
                new_media = resolve(new, new_resp).get("content", {}).get("application/json", {})
                if old_media.get("schema") and new_media.get("schema"):
                    compare_schema(old, old_media["schema"], new, new_media["schema"], f"response {status}", False, local)
            findings += [(rule, op_id, where, detail) for rule, where, detail in local]
    return findings


def key_of(module: str, finding: tuple[str, str, str, str]) -> str:
    """The waiver key of a finding: `<module> <rule> <METHOD path> <where>`, with trailing space removed when `where` is empty."""
    rule, op_id, where, detail = finding
    return f"{module} {rule} {op_id} {where}".rstrip()


def base_tag() -> str:
    """The newest `smo-v*` release tag that is not a release candidate. Ends the script with a message when there is none (typically a checkout without tags)."""
    tags = subprocess.run(["git", "-C", str(REPO), "tag", "--list", "smo-v*", "--sort=-version:refname"], capture_output=True, text=True, check=True).stdout.split()
    tags = [t for t in tags if "-rc." not in t]
    if not tags:
        sys.exit("no release tag found (does the checkout fetch tags?)")
    return tags[0]


def require_base(ref: str) -> None:
    """Ends the script with an error unless `ref` names a commit this checkout can resolve.

    Without this a `--base` that does not resolve (a typo, a tag that was not fetched, a shallow clone) makes `spec_at` and `specs_at` answer "nothing there", the comparison
    then covers no module and the gate passes having checked nothing (SEC-15.15). The job in `smo-tests.yml` fetches tags and full history and passes no `--base`, so it takes the
    newest tag (`base_tag`) and this check holds it to the same rule.
    """
    result = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"error: the base {ref!r} does not resolve to a commit in this checkout, so the R1 contract was not compared with anything "
                 "(a typo, a tag that was not fetched, or a shallow clone: fetch tags and history, or pass a ref that exists)")


def spec_at(ref: str, name: str) -> dict | None:
    """The OpenAPI document `smo/docs/openapi/<name>` as committed at git `ref`, or None when the file does not exist there (a module the older release did not have)."""
    result = subprocess.run(["git", "-C", str(REPO), "show", f"{ref}:smo/docs/openapi/{name}"], capture_output=True, text=True)
    return json.loads(result.stdout) if result.returncode == 0 else None


def specs_at(ref: str) -> list[str]:
    """The names of the OpenAPI JSON files committed at git `ref`. A ref that git cannot resolve gives an empty list rather than an error."""
    result = subprocess.run(["git", "-C", str(REPO), "ls-tree", "--name-only", f"{ref}:smo/docs/openapi/"], capture_output=True, text=True)
    return [line for line in result.stdout.split() if line.endswith(".json")]


def main() -> int:
    """Compares the committed specs at the base (`--base`, default the newest release tag) with the working tree and prints every unwaived break and stale waiver.

        Returns 1 when there is an unwaived break or a stale waiver, unless `--list` (print and return 0). A waiver key is matched with shell wildcards against the break key;
        every waiver that matched nothing is stale and fails the run, so the waiver file cannot outlive the release it was written for.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", help="git ref to compare with (default: the newest smo-v* tag that is not a release candidate)")
    parser.add_argument("--list", action="store_true", help="print every break with its waiver key and exit 0")
    args = parser.parse_args()
    base = args.base or base_tag()
    require_base(base)
    base_specs = specs_at(base)
    if not base_specs:
        # A base that resolves but holds no OpenAPI documents (older than `docs/openapi`) would compare nothing and pass; that is the same silent pass as an unresolved ref.
        sys.exit(f"error: the base {base!r} has no smo/docs/openapi/*.json, so there is nothing to compare the contract with")
    waivers = json.loads(WAIVERS_FILE.read_text()) if WAIVERS_FILE.exists() else {}
    used: set[str] = set()
    breaks = []
    for name in base_specs:
        if not (SMO / "docs" / "openapi" / name).exists():
            key = f"{name.removesuffix('.json')} removed-module"
            matching = [w for w in waivers if fnmatch.fnmatchcase(key, w)]
            if matching:
                used.update(matching)
            else:
                breaks.append((key, "the module's spec is gone"))
    for path in sorted((SMO / "docs" / "openapi").glob("*.json")):
        old = spec_at(base, path.name)
        if old is None:
            continue  # a module the previous release did not have
        for finding in compare_specs(path.stem, old, json.loads(path.read_text())):
            key = key_of(path.stem, finding)
            matching = [w for w in waivers if fnmatch.fnmatchcase(key, w)]
            if matching:
                used.update(matching)
            else:
                breaks.append((key, finding[3]))
    stale = sorted(set(waivers) - used)
    print(f"compared with {base}: {len(breaks)} unwaived break(s), {len(used)} waived, {len(stale)} stale waiver(s)")
    for key, detail in breaks:
        print(f"  BREAK  {key}  ({detail})")
    for key in stale:
        print(f"  STALE  {key}")
    if args.list:
        return 0
    return 1 if breaks or stale else 0


if __name__ == "__main__":
    sys.exit(main())
