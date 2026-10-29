# Wealthfolio Broker Sync as a container image (waldonso2/wealthfolio-importer-addon#148).
# Everything from requirements.txt with hashes and wheels only (#58), Scalable's sc only from its signed release
# (sc_install), a user without rights, no key in the image: it comes as the secret brokersync-key.
# Build: docker build -t brokersync .   Run: deploy/docker-compose.example.yml
ARG BASE=python:3.13-slim-trixie@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb

FROM ${BASE} AS build
# 1: the build fails without Scalable's sc (the published image); 0: an image without Scalable (local tests).
ARG REQUIRE_SC=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
COPY requirements.txt pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m venv /opt/brokersync \
 && /opt/brokersync/bin/pip install --require-hashes --only-binary :all: -r requirements.txt \
 && /opt/brokersync/bin/pip install --no-deps --no-build-isolation .
RUN /opt/brokersync/bin/brokersync install-sc --dir /opt/brokersync/bin || [ "$REQUIRE_SC" = 0 ]

FROM ${BASE}
COPY --from=build /opt/brokersync /opt/brokersync
RUN useradd --system --uid 10001 --user-group --home-dir /data --no-create-home --shell /usr/sbin/nologin brokersync \
 && mkdir -p /data /tls \
 && chown 10001:10001 /data /tls \
 && chmod 0700 /data /tls
ENV PATH=/opt/brokersync/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    BROKERSYNC_CONTAINER=1 \
    BROKERSYNC_DATA=/data \
    BROKERSYNC_TLS_DIR=/tls \
    BROKERSYNC_KEY_FILE=/run/secrets/brokersync-key \
    BROKERSYNC_SC=/opt/brokersync/bin/sc
USER 10001:10001
VOLUME ["/data", "/tls"]
# 8443: the UI over HTTPS (own CA, /tls), 8090: redirect - or the UI itself with BROKERSYNC_TLS=off behind a proxy.
EXPOSE 8443 8090
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
  CMD ["python", "-c", "import http.client as h,sys; c=h.HTTPConnection('127.0.0.1',8090,timeout=5); c.request('GET','/healthz'); sys.exit(c.getresponse().status>=500)"]
ENTRYPOINT ["brokersync"]
CMD ["container"]
