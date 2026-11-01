# One Dockerfile for all sixteen services — they share one stack
# (Python + FastAPI + SQLAlchemy), so a per-service Dockerfile would
# just be fourteen near-identical copies. MODULE picks which service's
# app/ directory gets copied in and run.
# PR-CONV-S1.6: the vendored 3GPP OpenAPI files (`specs/5G_APIs`), which the ProvMnS class registry of `smo_shared.mns` reads at run time. They are outside
# this build context (smo/), so a build passes them as the named context `specs` (`--build-context specs=../specs/5G_APIs`; docker-compose.yml's
# `additional_contexts`, and `scripts/ci_mirror.py build-contexts` for every CI build). This empty stage is what a build without it gets: no specs, and a
# module that mounts the router (Echo) then refuses to start, rather than serving no class.
FROM scratch AS specs

FROM python:3.14-slim@sha256:0741d101873c12ab927e6f8653feb8862b9bd58771177acb1b885b95141f91b4

ARG MODULE
ENV MODULE=${MODULE}

WORKDIR /srv

# Third-party packages come from the hashed, pinned lock (requirements/); the
# shared library itself is installed without re-resolving them.
COPY requirements/runtime.txt /srv/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /srv/requirements.txt
# Distributed traces (PR-OBS-3) are optional: the OpenTelemetry SDK and OTLP exporter are installed only with --build-arg WITH_TRACING=1
# (docker compose: SMO_WITH_TRACING=1 docker compose --profile tracing up --build). Without them the services still pass a traceparent on.
ARG WITH_TRACING=0
COPY requirements/tracing.txt /srv/requirements-tracing.txt
RUN if [ "$WITH_TRACING" = "1" ]; then pip install --no-cache-dir --require-hashes -r /srv/requirements-tracing.txt; fi
# The Kafka client of the event-bus relay (ADR 0006 slice 1, D9) is optional the same way: installed only with --build-arg WITH_KAFKA=1 (docker compose:
# SMO_WITH_KAFKA=1 docker compose --profile kafka up --build). Without it the images are unchanged, and a worker told to relay (SMO_KAFKA_RELAY=true)
# refuses to start, naming this argument (smo_shared/kafka_relay.py).
ARG WITH_KAFKA=0
COPY requirements/kafka.txt /srv/requirements-kafka.txt
RUN if [ "$WITH_KAFKA" = "1" ]; then pip install --no-cache-dir --require-hashes -r /srv/requirements-kafka.txt; fi

COPY shared /srv/shared
RUN pip install --no-cache-dir --no-deps -e /srv/shared
# the registry finds the nearest specs/5G_APIs above smo_shared: /srv/specs/5G_APIs
COPY --from=specs / /srv/specs/5G_APIs/

# Image vulnerability scan (PR-SEC-12.2): take the Debian security fix for pcre2 that the pinned base predates, and
# drop pip/setuptools/wheel, which nothing needs at run time and whose vendored copies of msgpack, urllib3 and
# setuptools carried the other findings. Remove this once a newer base digest has both fixed.
RUN apt-get update \
 && apt-get install -y --only-upgrade --no-install-recommends libpcre2-8-0 \
 && rm -rf /var/lib/apt/lists/* \
 && pip uninstall -y pip setuptools wheel
# The AI Runtime SDK (smo_sdk) — what a sample rApp (MODULE=samples/...)
# builds on; the SMO modules themselves never import it.
COPY sdk /srv/sdk

# The schema migrations travel in every image, so the image of any release can bring the database to that release's
# schema: docker compose's `migrate` service runs `python /srv/scripts/migrate.py` from one of them (PR-OPS-1.5).
COPY alembic.ini /srv/alembic.ini
COPY migrations /srv/migrations
COPY scripts/migrate.py /srv/scripts/migrate.py
COPY scripts/db_roles.py /srv/scripts/db_roles.py

COPY ${MODULE}/pyproject.toml* /srv/module/
COPY ${MODULE}/app /srv/module/app
WORKDIR /srv/module

# Run as an unprivileged user (PR-SEC-13.1). The code under /srv is root-owned and read-only to it; the only
# places a service writes are /tmp (a tmpfs) and the directories below, which compose mounts volumes on (a new named volume
# takes its ownership from the image, so the user can write to it): /data (the GUI BFF's database and
# first-run admin password) and /srv/packages (Onboarding's package store). A volume created by an earlier,
# root-run stack stays root-owned: recreate it (`docker compose down -v`, which discards demo data).
RUN groupadd --system --gid 10001 smo \
 && useradd --system --uid 10001 --gid smo --no-create-home --shell /usr/sbin/nologin smo \
 && mkdir -p /data /srv/packages /srv/scratch \
 && chown smo:smo /data /srv/packages /srv/scratch
USER 10001:10001

ENV PYTHONPATH=/srv/module:/srv/sdk
EXPOSE 8000

# Build identity, answered by GET /version (PR-OBS-8.1). Declared last so a new commit invalidates only this layer, not
# the dependency and code layers above. docker-compose.yml passes ${SMO_VERSION}, ${SMO_BUILD_SHA} and ${SMO_BUILT_AT}
# from the environment; the release workflow passes the tag, the commit and the build time.
ARG SMO_VERSION=unknown
ARG SMO_BUILD_SHA=unknown
ARG SMO_BUILT_AT=unknown
ENV SMO_VERSION=${SMO_VERSION} \
    SMO_BUILD_SHA=${SMO_BUILD_SHA} \
    SMO_BUILT_AT=${SMO_BUILT_AT}

# Worker processes per container, and how long a stopping container keeps finishing the requests it already
# accepted (PR-ST-6). On SIGTERM uvicorn stops accepting connections, lets in-flight requests complete, and
# exits; anything still running after UVICORN_GRACEFUL_SHUTDOWN_SECONDS is cut. docker-compose.yml gives every
# service a stop_grace_period longer than that, so docker's own SIGKILL never lands first. Workers hold no
# shared state (docs/ARCHITECTURE.md, Process state and scale-out), so a service may raise UVICORN_WORKERS; the
# two mock services keep their state in memory and must stay at 1. `exec` makes uvicorn PID 1, so it receives
# the stop signal itself. Mutual TLS (PR-SEC-2, off unless SMO_MTLS=on): `python -m smo_shared.mtls uvicorn-args` prints the TLS options (certificate, key, CA,
# a client certificate required) and nothing otherwise; if it fails (mTLS on, a file missing) the container exits instead of serving plain HTTP. `--no-access-log` turns off uvicorn's plain-text access line: smo_shared/logconfig.py logs
# one structured line per request instead (PR-OBS-1).
ENV UVICORN_WORKERS=1 \
    UVICORN_GRACEFUL_SHUTDOWN_SECONDS=20
CMD ["sh", "-c", "tls=$(python -m smo_shared.mtls uvicorn-args) || exit 1; exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS} --timeout-graceful-shutdown ${UVICORN_GRACEFUL_SHUTDOWN_SECONDS} --no-access-log $tls"]
