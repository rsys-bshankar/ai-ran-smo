---
section: Changed
---
- **For contributors: each module declares itself in `<module>/module.yaml`, and the files that listed every module are generated from those
  manifests** (`scripts/modules.py`, schema `scripts/module.schema.json`). `docker-compose.yml` and the chart's `values.yaml` keep the same services
  and values (`docker compose config` and the parsed values are unchanged), but their module services now sit in generated regions, one per
  module in path order: edit the module's `module.yaml` and run `python scripts/modules.py --write`, not the region. `migrations/db_roles.json`
  and `table_owners.json` are generated (same content, keys in path order), R1 Termination reads its static routes from the generated
  `r1-termination/app/routes.json` (same prefixes, variables and defaults), and `coverage_floors.json` is gone: a module's coverage floor is
  `tests.floor` in its manifest.
