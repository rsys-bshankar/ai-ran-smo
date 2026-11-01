# Code documentation baseline (`scripts/check_code_docs.py`)

One file per module (`scripts/baseline_dirs.py`; `(root)` is `_root.json`): what the documentation rule (`docs/CODE_DOCUMENTATION.md`) still asks of that module. A module whose count rises above its file fails the gate; a count that went down is lowered with `python scripts/check_code_docs.py --write-baseline`, in the same change, which rewrites that module's file only.
