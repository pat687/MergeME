# syntax=docker/dockerfile:1
#
# OPTIONAL: eXo Platform Community + the "lead-capture" add-on, built from source.
# Only needed if you turn on ROUTER_EXO_SYNC_ENABLED. See infra/exo/README.md,
# especially the version-matching table: the add-on must target your eXo version.
#
#   docker build \
#     --build-arg EXO_IMAGE=exoplatform/exo-community:7.2.0 \
#     --build-arg LEADCAPTURE_REF=develop \
#     -t barrgo/exo-leadcapture:7.2.0 infra/exo

ARG EXO_IMAGE=exoplatform/exo-community:7.2.0
ARG LEADCAPTURE_REF=develop

# ---- 1. Compile the add-on ----------------------------------------------------
FROM maven:3.9-eclipse-temurin-21 AS addon
ARG LEADCAPTURE_REF
ADD https://github.com/exo-addons/lead-capture.git#${LEADCAPTURE_REF} /src
COPY settings.xml /root/.m2/settings.xml
WORKDIR /src
RUN mvn -B -ntp package -DskipTests \
 && mkdir /addon \
 && cd /addon \
 && jar xf /src/packaging/target/lead-capture.zip

# ---- 2. Drop it into eXo (an add-on is just WARs + JARs) -----------------------
FROM ${EXO_IMAGE}
COPY --from=addon --chown=exo:exo /addon/webapps/ /opt/exo/webapps/
COPY --from=addon --chown=exo:exo /addon/lib/ /opt/exo/lib/
