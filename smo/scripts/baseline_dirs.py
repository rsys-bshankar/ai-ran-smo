"""A gate's baseline as a directory of one JSON file per module, so two pull requests on different modules never edit the same baseline file
(smo/CLAUDE.md, "Working in parallel"). Used by `check_interfaces.py` (scripts/interface_baseline/), `check_contract_drift.py`
(scripts/contract_drift_baseline/) and `check_code_docs.py` (scripts/code_docs_baseline/).

Each file is `<slug>.json` holding `{"module": "<name>", ...that module's part of the baseline}`: the name is read from the file, the slug (the name
with `/` written `--` and `(root)` written `_root`) only keeps file names plain. A directory's `README.md` says what its gate is and how it may change.
Writing touches only the files whose content changed and removes the file of a module that has nothing left, so "the baseline only shrinks" stays a
per-module property a reviewer can see in the diff. Standard library only.
"""

import json
import subprocess
from pathlib import Path


def slug(module: str) -> str:
    """The file name stem of a module's baseline: `/` written `--`, `(root)` written `_root`."""
    return "_root" if module == "(root)" else module.replace("/", "--")


def _render(module: str, part: dict) -> str:
    return json.dumps({"module": module, **part}, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def read(directory: Path) -> dict[str, dict]:
    """{module: its part of the baseline} from every `*.json` of `directory` (empty when the directory does not exist)."""
    out: dict[str, dict] = {}
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        data = json.loads(path.read_text(encoding="utf-8"))
        module = data.pop("module")
        if module in out:
            raise ValueError(f"{path}: module {module!r} has two baseline files")
        out[module] = data
    return out


def write(directory: Path, parts: dict[str, dict]) -> tuple[list[str], list[str]]:
    """Writes `parts` ({module: its part}) to `directory`, one file per module with something in it; returns (written, removed) file names."""
    directory.mkdir(parents=True, exist_ok=True)
    wanted = {f"{slug(m)}.json": _render(m, part) for m, part in parts.items() if part}
    written, removed = [], []
    for name, text in sorted(wanted.items()):
        path = directory / name
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            written.append(name)
    for path in sorted(directory.glob("*.json")):
        if path.name not in wanted:
            path.unlink()
            removed.append(path.name)
    return written, removed


def read_at(ref: str, directory: str, legacy_file: str | None, cwd: Path) -> dict[str, dict] | None:
    """The baseline at git `ref` ({module: part}): the directory `directory` (a path from `cwd`), or, before the split, `legacy_file` read whole and
    returned as {"": its content} for the caller to split. None when neither exists at `ref`."""
    # --full-tree: run from a subdirectory (smo/), ls-tree would list only the entries under that prefix, which a tree object has none of
    listed = subprocess.run(["git", "ls-tree", "--full-tree", "--name-only", f"{ref}:{directory}"], capture_output=True, text=True, cwd=cwd)
    if listed.returncode == 0:
        out = {}
        for name in listed.stdout.split():
            if name.endswith(".json"):
                shown = subprocess.run(["git", "show", f"{ref}:{directory}/{name}"], capture_output=True, text=True, cwd=cwd, check=True)
                data = json.loads(shown.stdout)
                out[data.pop("module")] = data
        return out
    if legacy_file:
        shown = subprocess.run(["git", "show", f"{ref}:{legacy_file}"], capture_output=True, text=True, cwd=cwd)
        if shown.returncode == 0:
            return {"": json.loads(shown.stdout)}
    return None
