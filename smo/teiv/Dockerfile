# O-RAN-SC smo-teiv 0.3.0 (Release M), built from its pinned source for the TE&IV spike (plan task S0.13, ADR 0013). Not part of the stack.
#
# O-RAN-SC's last container release of smo-teiv is 0.2.0 (L release); 0.3.0 was released as Maven artifacts only, so the image is built here, as
# smo/oran-sim/ builds the O1 simulator. The named build context `teiv` is the upstream repository at the 0.3.0 tag's commit (docker-compose.yml).
# One image serves both of smo-teiv's processes: Spring profile `ingestion` (Kafka consumer, the only writer of the store) or `exposure,groups`
# (the REST API); docker-compose.yml chooses. Upstream's Dockerfiles install OpenJDK 17 into an unpinned debian:bullseye-slim; this one compiles with
# the pinned Maven image (JDK 21, target 17) and runs on a pinned Temurin 21 JRE, both listed in deploy/mirror/images.txt.
#
# The TE&IV extension spike (plan task S3.20, ADR 0013 section 11): the image carries TE&IV's own model files, spikes/teiv/model/*.yang, beside
# smo-teiv's in the jar's models/ (smo-teiv's ingestion validates each event against the YANG modules it finds there whose names its
# teiv_model.module_reference table lists, so a model added to the database only is not validated and is refused). No Java file of smo-teiv is
# changed; model/build-model.sh only adds files. The database and its schema are Dockerfile.db's.

FROM maven:3.9.11-eclipse-temurin-21-noble@sha256:6fdc855a6ed81d288ca7ca37ac6ff5e9308b612485c0801d70b25a858c83d237 AS build
WORKDIR /src
COPY --from=teiv . .
COPY maven-settings.xml /tmp/maven-settings.xml
COPY model /tmp/model
RUN sh /tmp/model/build-model.sh models /src /tmp/model
# maven-settings.xml sends every repository smo-teiv's POMs name to Maven Central (on a GitHub runner the others answer, and the build then failed to
# find swagger-annotations and commons-lang3 on its compile classpath). -Dexec.skip: the teiv module's build runs the schema generator only to write
# SQL into its test resources (the runtime schema is Dockerfile.db's); tests are skipped, so it is not needed. -Dmaven.antrun.skip: antrun only downloads YANG modules from www.ietf.org and
# forge.3gpp.org (unpinned, ADR 0013 section 3); the import modules smo-teiv needs at run time are already in models/import (build-model.sh).
RUN mvn -B --no-transfer-progress -s /tmp/maven-settings.xml -pl teiv -am -DskipTests -Dexec.skip=true -Dmaven.antrun.skip=true -Djacoco.skip=true -Dspotless.check.skip=true -Dspotless.apply.skip=true package \
 && cp "$(ls teiv/target/topology-exposure-inventory-jar-*.jar | grep -v -- -stubs)" /app.jar

FROM eclipse-temurin:21-jre-noble@sha256:161310cd5bd41fd549d19cf9f90b614897cb4a1532555c613b43b7e0628fe5d6
WORKDIR /opt/app/teiv
COPY --from=build /app.jar /opt/app/teiv/app.jar
COPY --from=teiv teiv/src/main/resources/application.yaml /opt/app/teiv/config/application.yaml
USER 40514
CMD ["java", "-jar", "/opt/app/teiv/app.jar"]
