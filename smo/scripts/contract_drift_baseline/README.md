# Contract drift baseline (`scripts/check_contract_drift.py`)

One file per module (`scripts/baseline_dirs.py`): the module's `code_only` and `contract_only` counts. Written by `python scripts/check_contract_drift.py --write-baseline`; counts may only go down (plan task S0.7). The check fails when a module's count grows above its file.
