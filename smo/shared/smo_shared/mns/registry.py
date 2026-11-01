"""The class registry (Component B, plan task S1.2): which classes exist, what attributes each may hold, and what may contain what, read from the vendored
3GPP OpenAPI YAML (`specs/5G_APIs`).

A class is a `<Class>-Single` schema of a loaded file. By default the registry indexes every TS 28.541 file (`TS28541_*.yaml`: NR, 5GC, slicing) and
every TS 28.623 file (`TS28623_*.yaml`: the generic NRM, PM, trace, threshold, subscription and file management, the MnS registry, ...); a module that
serves classes of another specification adds its file (`index_file("TS28104_MdaNrm.yaml")`). Files a schema refers to are loaded when a `$ref` reaches
them, whatever their prefix.

For each class the registry derives, from the `-Single` schema:

  attributes    the schemas of its `attributes` member (every `allOf` branch that has one, following `$ref`), combined with `allOf`. `validate` checks an
                instance's attributes against them with a JSON Schema validator (Draft 7, whose `$ref` ignores its siblings, as OpenAPI 3.0 does), after
                turning OpenAPI's `nullable: true` into JSON Schema (`null` allowed). An attribute name that none of the schemas declares is refused too:
                the NRM schemas do not say `additionalProperties: false`, and a misspelt attribute would otherwise be stored silently.
  children      the classes it name-contains: every other member (or `allOf` branch) that refers to an `X-Single` or `X-Multiple` schema, following the
                `-Attr` and `-ncO` composition schemas, in whichever file they are. `SubNetwork-Single` lists the `-ncO` schemas of 25 specifications;
                that is how `ManagedElement` gets `GNBDUFunction` (TS 28.541) as a child. `may_contain(parent, child)` reads this.
  roots         classes that may be created with no parent (`SubNetwork`, `ManagedElement` by default; `roots=` changes it).

A class may also be registered by hand with its own attributes schema (`register`), for a class no 3GPP file defines (an ADR 0011 [D] class such as
`RappInstance`, whose schema is in the bundle's module contract).

Nothing here talks to a database or the network; the YAML is read once per registry, lazily, from `spec_dir` (by default the repository's
`specs/5G_APIs`, found relative to this file).
"""

import copy
import functools
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft7Validator
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource, Unresolvable
from referencing.jsonschema import DRAFT7

from .errors import MnsError

BASE_URI = "https://smo.invalid/specs/5G_APIs/"


def _find_spec_dir(start: Path) -> Path:
    """The nearest `specs/5G_APIs` above `start` (the repository's, wherever the package was copied to: an editable install, mutmut's `mutants/`);
    the repository layout's place when there is none, which then reads as missing files."""
    for parent in start.parents:
        candidate = parent / "specs" / "5G_APIs"
        if candidate.is_dir():
            return candidate
    return start.parents[min(4, len(start.parents) - 1)] / "specs" / "5G_APIs"


DEFAULT_SPEC_DIR = _find_spec_dir(Path(__file__).resolve())
DEFAULT_CLASS_FILES = ("TS28541_*.yaml", "TS28623_*.yaml")
DEFAULT_ROOTS = ("SubNetwork", "ManagedElement")
_IDENTITY_MEMBERS = ("id", "objectClass", "objectInstance")
_CLASS_REF = re.compile(r"^(?P<file>[^#]*)#/components/schemas/(?P<name>[A-Za-z][A-Za-z0-9_]*)-(?:Single|Multiple)$")
_SCHEMA_REF = re.compile(r"^(?P<file>[^#]*)#(?P<pointer>/.*)$")


def openapi_to_jsonschema(node: Any) -> Any:
    """A copy of an OpenAPI 3.0 schema tree in which `nullable: true` is JSON Schema: `null` added to `type` (and to `enum`), or, next to a `$ref` or
    without a `type`, an `anyOf` with `{"type": "null"}`. Everything else is kept as it is."""
    if isinstance(node, list):
        return [openapi_to_jsonschema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {key: openapi_to_jsonschema(value) for key, value in node.items()}
    if out.pop("nullable", False) is not True:
        return out
    if "$ref" in out or "type" not in out:
        return {"anyOf": [out, {"type": "null"}]}
    types = out["type"] if isinstance(out["type"], list) else [out["type"]]
    out["type"] = [*types, "null"]
    if "enum" in out and None not in out["enum"]:
        out["enum"] = [*out["enum"], None]
    return out


@dataclass
class ClassInfo:
    """What the registry knows of one class: where its `-Single` schema is, its attribute schemas, the attribute names they declare, its children."""

    name: str
    source: str                                       # "TS28541_NrNrm.yaml" or "registered"
    attribute_refs: list[str] = field(default_factory=list)     # absolute $ref URIs of the attribute schemas
    flat_refs: dict[str, str] = field(default_factory=dict)     # attribute name -> $ref URI, for attributes the YAML declares outside `attributes`
    attribute_schema: dict | None = None              # a registered class's own schema (instead of attribute_refs)
    attribute_names: frozenset[str] = frozenset()
    children: frozenset[str] = frozenset()


class ClassRegistry:
    """The classes, their attribute schemas and containment, from the YAML of `spec_dir` (see the module description)."""

    def __init__(self, spec_dir: Path | str | None = None, class_files: Iterable[str] = DEFAULT_CLASS_FILES, roots: Iterable[str] = DEFAULT_ROOTS):
        """Indexes the `-Single` schemas of every file of `spec_dir` that matches one of `class_files`; derives nothing yet (that happens per class, on first use)."""
        self.spec_dir = Path(spec_dir) if spec_dir is not None else DEFAULT_SPEC_DIR
        self.roots = frozenset(roots)
        self.missing_files: set[str] = set()
        self._documents: dict[str, dict] = {}
        self._index: dict[str, str] = {}                # class name -> the file of its -Single schema
        self._classes: dict[str, ClassInfo] = {}
        self._registered: dict[str, ClassInfo] = {}
        self._validators: dict[str, Draft7Validator] = {}
        self._schema_registry: Registry = Registry(retrieve=self._retrieve)
        for pattern in class_files:
            for path in sorted(self.spec_dir.glob(pattern)):
                self.index_file(path.name)

    # ------------------------------------------------------------------ loading

    def document(self, file_name: str) -> dict:
        """The parsed, `nullable`-converted YAML file `file_name` of `spec_dir`; an empty document (recorded in `missing_files`) when there is none."""
        if file_name not in self._documents:
            path = self.spec_dir / file_name
            if path.is_file():
                with path.open(encoding="utf-8") as handle:
                    self._documents[file_name] = openapi_to_jsonschema(yaml.safe_load(handle) or {})
            else:
                self.missing_files.add(file_name)
                self._documents[file_name] = {}
        return self._documents[file_name]

    def _retrieve(self, uri: str) -> Resource:
        """`referencing` retrieval: a URI under BASE_URI is that YAML file; anything else (or a file that is not there) is unresolvable."""
        if not uri.startswith(BASE_URI):
            raise NoSuchResource(ref=uri)
        name = uri[len(BASE_URI):]
        contents = self.document(name)
        if not contents:
            raise NoSuchResource(ref=uri)
        return DRAFT7.create_resource(contents)

    def index_file(self, file_name: str) -> int:
        """Adds every `X-Single` schema of `file_name` as class `X` (a class already indexed from another file keeps its first file); returns how many
        it added."""
        added = 0
        for name in self.document(file_name).get("components", {}).get("schemas", {}):
            if name.endswith("-Single"):
                class_name = name[:-len("-Single")]
                if class_name not in self._index:
                    self._index[class_name] = file_name
                    added += 1
        return added

    def register(self, name: str, attributes_schema: dict, children: Iterable[str] = ()) -> None:
        """Registers class `name` with its own JSON Schema for `attributes` (OpenAPI `nullable` is converted) and the classes it may contain. It replaces
        a YAML class of the same name."""
        schema = openapi_to_jsonschema(attributes_schema)
        self._registered[name] = ClassInfo(name, "registered", attribute_schema=schema, attribute_names=frozenset(self._declared_names(schema, None)),
                                           children=frozenset(children))
        self._classes.pop(name, None)
        self._validators.pop(name, None)

    # ------------------------------------------------------------------ reading

    def names(self) -> list[str]:
        """Every class the registry knows (indexed from the YAML or registered), sorted."""
        return sorted(set(self._index) | set(self._registered))

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and (name in self._registered or name in self._index)

    def info(self, name: str) -> ClassInfo:
        """The `ClassInfo` of `name`; a class the registry does not know is a 422 CLASS_NOT_SUPPORTED."""
        if name in self._registered:
            return self._registered[name]
        if name not in self._index:
            raise MnsError(422, "CLASS_NOT_SUPPORTED", f"no schema is registered for class {name!r}", bad_objects=[name])
        if name not in self._classes:
            self._classes[name] = self._derive(name, self._index[name])
        return self._classes[name]

    def may_contain(self, parent: str | None, child: str) -> bool:
        """Whether an instance of `child` may be created under an instance of `parent` (None: at the top, which only the roots may)."""
        if parent is None:
            return child in self.roots
        return parent in self and child in self.info(parent).children

    def validate(self, name: str, attributes: Any) -> None:
        """Checks `attributes` against class `name`; raises a 422 SCHEMA_VALIDATION_FAILED naming the attributes at fault (unknown names first)."""
        info = self.info(name)
        if not isinstance(attributes, dict):
            raise MnsError(422, "SCHEMA_VALIDATION_FAILED", "attributes must be an object", bad_attributes=["attributes"])
        unknown = sorted(set(attributes) - info.attribute_names)
        if unknown:
            raise MnsError(422, "SCHEMA_VALIDATION_FAILED", f"class {name} has no attribute {', '.join(unknown)}", bad_attributes=unknown)
        errors = sorted(self._validator(name).iter_errors(attributes), key=lambda e: [str(p) for p in e.absolute_path])
        if errors:
            bad = sorted({str(e.absolute_path[0]) if e.absolute_path else "attributes" for e in errors})
            raise MnsError(422, "SCHEMA_VALIDATION_FAILED", f"class {name}: {errors[0].message}", bad_attributes=bad)

    def resolvable(self, name: str) -> bool:
        """Whether every `$ref` reachable from class `name`'s attribute schemas resolves (so `validate` can never stop at a missing file or schema)."""
        try:
            info = self.info(name)
        except MnsError:
            return False
        if info.attribute_schema is not None:
            return True
        seen: set[str] = set()
        pending = [*info.attribute_refs, *info.flat_refs.values()]
        while pending:
            uri = pending.pop()
            if uri in seen:
                continue
            seen.add(uri)
            try:
                resolved = self._schema_registry.resolver().lookup(uri)
            except Unresolvable:
                return False
            base = uri.split("#", 1)[0]
            pending.extend(self._refs_in(resolved.contents, base))
        return True

    # ------------------------------------------------------------------ deriving

    @staticmethod
    def _refs_in(node: Any, base: str) -> list[str]:
        """Every `$ref` in a schema tree, made absolute against the file URI `base`."""
        found: list[str] = []
        stack = [node]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                ref = current.get("$ref")
                if isinstance(ref, str):
                    found.append(ref if ref.startswith(BASE_URI) else (base + ref if ref.startswith("#") else BASE_URI + ref))
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)
        return found

    def _lookup(self, file_name: str, ref: str) -> tuple[str, str, Any] | None:
        """The (file, JSON pointer, node) a `$ref` written in `file_name` points at, or None when it does not resolve."""
        match = _SCHEMA_REF.match(ref)
        if not match:
            return None
        target_file = match["file"] or file_name
        node: Any = self.document(target_file)
        for token in match["pointer"].split("/")[1:]:
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(node, list) and token.isdigit() and int(token) < len(node):
                node = node[int(token)]
            elif isinstance(node, dict) and token in node:
                node = node[token]
            else:
                return None
        return target_file, match["pointer"], node

    def _derive(self, name: str, file_name: str) -> ClassInfo:
        """Walks `<name>-Single` of `file_name` through `allOf` and `$ref` (not into member values), collecting the attribute schemas and children."""
        attribute_refs: list[str] = []
        flat_refs: dict[str, str] = {}
        children: set[str] = set()
        seen: set[tuple[str, str]] = set()

        def child_of(ref: str) -> str | None:
            match = _CLASS_REF.match(ref)
            return match["name"] if match else None

        def walk(file: str, pointer: str, node: Any) -> None:
            if not isinstance(node, dict) or (file, pointer) in seen:
                return
            seen.add((file, pointer))
            ref = node.get("$ref")
            if isinstance(ref, str):
                child = child_of(ref)
                if child is not None and pointer != f"/components/schemas/{name}-Single":
                    children.add(child)               # an allOf branch that is itself a contained class (SubNetwork-Single lists two)
                    return
                target = self._lookup(file, ref)
                if target is None:
                    return
                if target[1].endswith("-Attr"):
                    attribute_refs.append(f"{BASE_URI}{target[0]}#{target[1]}")   # `<Class>-Attr` is the attribute set itself (SubNetwork, ManagedElement)
                    return
                walk(*target)
                return
            for index, branch in enumerate(node.get("allOf", [])):
                walk(file, f"{pointer}/allOf/{index}", branch)
            for member, value in node.get("properties", {}).items():
                if member == "attributes":
                    attribute_refs.append(f"{BASE_URI}{file}#{pointer}/properties/attributes")
                elif member in _IDENTITY_MEMBERS:
                    continue
                elif isinstance(value, dict) and isinstance(value.get("$ref"), str) and child_of(value["$ref"]) is not None:
                    children.add(str(child_of(value["$ref"])))
                elif isinstance(value, dict) and not member[0].isupper():
                    # an attribute written next to `attributes` instead of in it, as some -Single schemas do (NROperatorCellDU, BWPSet)
                    flat_refs[member] = f"{BASE_URI}{file}#{pointer}/properties/{member.replace('~', '~0').replace('/', '~1')}"

        walk(file_name, f"/components/schemas/{name}-Single", self.document(file_name)["components"]["schemas"][f"{name}-Single"])
        names: set[str] = set(flat_refs)
        for uri in attribute_refs:
            names |= self._declared_names({"$ref": uri}, None)
        return ClassInfo(name, file_name, attribute_refs=attribute_refs, flat_refs=flat_refs, attribute_names=frozenset(names),
                         children=frozenset(children))

    def _declared_names(self, schema: Any, file: str | None) -> set[str]:
        """The property names a schema declares at its top level, through `allOf`, `anyOf`, `oneOf` and `$ref` (not into the properties' values)."""
        names: set[str] = set()
        stack: list[tuple[str | None, Any]] = [(file, schema)]
        seen: set[int] = set()
        while stack:
            current_file, node = stack.pop()
            if not isinstance(node, dict) or id(node) in seen:
                continue
            seen.add(id(node))
            names |= set(node.get("properties", {}))
            for keyword in ("allOf", "anyOf", "oneOf"):
                stack.extend((current_file, branch) for branch in node.get(keyword, []))
            ref = node.get("$ref")
            if isinstance(ref, str):
                if ref.startswith(BASE_URI):
                    target_file, _, fragment = ref[len(BASE_URI):].partition("#")
                    target = self._lookup(target_file, "#" + fragment)
                elif current_file is not None:
                    target = self._lookup(current_file, ref)
                else:
                    target = None
                if target is not None:
                    stack.append((target[0], target[2]))
        return names

    @staticmethod
    def _combined(info: ClassInfo) -> dict:
        """One schema for a YAML class's attributes: every `attributes` / `-Attr` schema, plus the attributes declared outside them."""
        parts: list[dict] = [{"$ref": uri} for uri in info.attribute_refs]
        if info.flat_refs:
            parts.append({"type": "object", "properties": {member: {"$ref": uri} for member, uri in info.flat_refs.items()}})
        return {"allOf": parts}

    def _validator(self, name: str) -> Draft7Validator:
        """The cached validator of class `name`'s attributes."""
        if name not in self._validators:
            info = self.info(name)
            schema = info.attribute_schema if info.attribute_schema is not None else self._combined(info)
            self._validators[name] = Draft7Validator(copy.deepcopy(schema), registry=self._schema_registry)
        return self._validators[name]


@functools.lru_cache(maxsize=4)
def default_registry(spec_dir: str | None = None) -> ClassRegistry:
    """One shared registry per spec directory (the YAML is large; parsing it once per process is enough)."""
    return ClassRegistry(spec_dir)
