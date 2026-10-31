# Python requirements

Every service container (`smo/Dockerfile`) and every CI job installs its
third-party packages from the hashed, pinned files here — never from an
unpinned list — so a build is reproducible and a changed or tampered package
fails the hash check.

| File | Used by |
|---|---|
| `runtime.in` / `runtime.txt` | service images, the migration check |
| `dev.in` / `dev.txt` | runtime + pytest: unit, integration and live-replay jobs |
| `lint.in` / `lint.txt` | the `lint` job (ruff, mypy) |
| `tracing.in` / `tracing.txt` | OpenTelemetry for distributed traces, optional (`--build-arg WITH_TRACING=1`); also part of `dev.txt` so CI runs the span tests |
| `mutation.in` / `mutation.txt` | mutmut, on top of `dev.txt`: the mutation-testing pilot (`scripts/mutation_pilot.sh`) |
| `dr.in` / `dr.txt` | the disaster-recovery drill (`.github/workflows/smo-dr.yml`): `moto_server`, an S3 stand-in, and the AWS CLI the `scripts/dr_*.sh` call; constrained by `dev.txt` so no pin there moves |
| `contracts.in` / `contracts.txt` | the "Contracts" job (`pyang --strict` on the schema bundle's YANG modules, plan task S0.7); constrained by `dev.txt` |
| `smoke.in` / `smoke.txt` | the deploy gate's headless GUI check (`scripts/gui_smoke.py`, Playwright) |

The `.in` files hold the direct dependencies; the `.txt` files are generated and
must not be edited by hand. Dependabot updates the `.txt` files weekly (its pip
ecosystem understands `pip-compile` output), CI runs on the result, and the
`smo/shared` library is installed with `--no-deps` on top.

Recompile after editing a `.in` file (Python 3.11, the oldest supported):

```bash
pip install pip-tools
cd smo/requirements
pip-compile --generate-hashes --strip-extras --allow-unsafe -o runtime.txt runtime.in
pip-compile --generate-hashes --strip-extras --allow-unsafe -o dev.txt dev.in
pip-compile --generate-hashes --strip-extras --allow-unsafe -o lint.txt lint.in
pip-compile --generate-hashes --strip-extras --allow-unsafe -o tracing.txt tracing.in
pip-compile --generate-hashes --strip-extras --allow-unsafe -o smoke.txt smoke.in
pip-compile --generate-hashes --strip-extras --allow-unsafe --constraint=dev.txt -o dr.txt dr.in
pip-compile --generate-hashes --strip-extras --allow-unsafe --constraint=dev.txt -o contracts.txt contracts.in
```

Install locally: `pip install --require-hashes -r requirements/dev.txt && pip install --no-deps -e shared`.
