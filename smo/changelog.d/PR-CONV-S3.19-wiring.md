---
section: Added
---
- **TE&IV runs in the stack (plan task S3.19).** Compose: the service `teiv` in the profile `teiv` (`docker compose --profile teiv up`), with smo-teiv, on the networks `default` and `teiv`, three uvicorn workers, its database role `smo_teiv` (new secret `secrets/db_password_teiv`, made by `scripts/init_secrets.sh`). Chart: `modules.teiv` (off by default; turn it on with `teiv.enabled`). R1 Termination forwards `/teiv/topology-inventory/v1/...` (the WG10 TE&IV R1 API) to it, new setting `TEIV_URL` (default `http://teiv:8000`); TE&IV's ProvMnS classes are routed by class name.
