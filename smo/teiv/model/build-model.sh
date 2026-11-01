#!/bin/sh
# The TE&IV extension spike's model step (plan task S3.20, ADR 0013 section 11), run inside the spike's image builds (spikes/teiv/Dockerfile and
# Dockerfile.db) on a checkout of O-RAN-SC smo-teiv 0.3.0. It changes model files only, never smo-teiv's Java code:
#
#   build-model.sh models SRC EXT   put the TE&IV extension YANG (EXT/*.yang) beside smo-teiv's own in SRC/teiv/src/main/resources/models, and the
#                                   five import modules smo-teiv's models need (3GPP common types and extensions, IETF inet, yang types and geo
#                                   location) in its models/import. The import modules are taken from smo-teiv's own committed schema
#                                   (docker-compose/sql_scripts/00_init-teiv-exposure-model.sql embeds each module's text, base64, in
#                                   teiv_model.module_reference), so they are exactly the revisions smo-teiv 0.3.0 was generated with, and the build
#                                   needs neither www.ietf.org nor forge.3gpp.org (upstream's Maven build downloads them; with the files present it
#                                   skips the download, and the spike also skips antrun).
#   build-model.sh schema SRC OUT   run smo-teiv's own schema generator (SRC/pgsql-schema-generator/target/pgsql-schema-generator-1.2.1.jar, built
#                                   from the tag) over that models directory, green field, and write the four SQL scripts into OUT under the names
#                                   upstream's docker-compose/sql_scripts uses, with the database user filled in as upstream's
#                                   copySqlSchemaFromPgsqlGenerator.sh does.
set -eu

mode=$1
src=$2
models="$src/teiv/src/main/resources/models"

case "$mode" in
models)
    ext=$3
    mkdir -p "$models/import"
    # module_reference rows: name, namespace, domain, includedModules, revision, content (tab separated); the import modules have no domain.
    awk -F '\t' '/^COPY teiv_model.module_reference/ { on = 1; next } /^\\\./ { on = 0 } on && $1 ~ /^(_3gpp|ietf)-/ { print $1, $NF }' \
        "$src/docker-compose/sql_scripts/00_init-teiv-exposure-model.sql" |
        while read -r name content; do
            printf '%s' "$content" | base64 -d > "$models/import/$name.yang"
        done
    [ "$(ls "$models/import" | wc -l)" -eq 5 ] || { echo "expected 5 import modules in $models/import" >&2; exit 1; }
    cp "$ext"/*.yang "$models/"
    ls "$models" "$models/import"
    ;;
schema)
    out=$3
    user=${4:-topology_exposure_user}
    work=$(mktemp -d)
    mkdir -p "$work/target" "$out"
    # the generator writes its temporary files under ./target, so it runs in a scratch directory
    (cd "$work" && java -jar "$src/pgsql-schema-generator/target/pgsql-schema-generator-1.2.1.jar" \
        --yang-model.external.source="$models" \
        --schema.data.output=target/00_init-oran-smo-teiv-data.sql \
        --schema.model.output=target/01_init-oran-smo-teiv-model.sql \
        --schema.consumer-data.output=target/02_init-oran-smo-teiv-consumer-data.sql \
        --schema.groups.output=target/03_init-oran-smo-teiv-groups.sql \
        --graphs.generate=false > generator.log 2>&1) || { tail -50 "$work/generator.log" >&2; exit 1; }
    for pair in 00_init-oran-smo-teiv-data:01_init-teiv-exposure-data 01_init-oran-smo-teiv-model:00_init-teiv-exposure-model \
                02_init-oran-smo-teiv-consumer-data:02_init-teiv-exposure-consumer-data 03_init-oran-smo-teiv-groups:03_init-teiv-exposure-groups; do
        sed "s/:pguser/$user/g; s/:'pguser'/'$user'/g" "$work/target/${pair%%:*}.sql" > "$out/${pair#*:}.sql"
    done
    ls -l "$out"
    ;;
*)
    echo "usage: build-model.sh models SRC EXT | schema SRC OUT [DB_USER]" >&2
    exit 2
    ;;
esac
