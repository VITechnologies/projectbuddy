# Linux SBOM generation image: NI's official LabVIEW container + VIPM CLI + Grype.
# Built and run by Run-LinuxSbom.ps1; see docs/sbom-pipeline/procedure.md.
ARG LABVIEW_IMAGE=nationalinstruments/labview:latest-linux
FROM ${LABVIEW_IMAGE}

# Pinned VIPM 2026 Q3 release (26.3.2 build 4056) from docs.vipm.io/2026-Q3/installation;
# the preview channel's "latest" URL lags behind it.
ARG VIPM_DEB_URL=https://traffic.libsyn.com/secure/jkinc/vipm_26.3.2-4056_amd64.deb
ARG VIPM_DEB_SHA256=89f50b8dc7ac023224f1b8f822ff99ac221ad31c67bae06951c8d1e09e7eef6c
ARG GRYPE_VERSION=0.121.0
ARG GRYPE_LINUX_AMD64_SHA256=64e07133d3486115de647a66581461dccf5c732748a0a3119077395565f15519
# "1" applies pending Ubuntu updates (the base image has only Ubuntu apt
# sources, so NI packages are unaffected); "0" keeps NI's image as published.
ARG APPLY_OS_UPDATES=1

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && if [ "${APPLY_OS_UPDATES}" = "1" ]; then \
           DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends; \
       fi \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates curl git subversion procps xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY install-vipm.sh /usr/local/sbom/install-vipm.sh
RUN bash /usr/local/sbom/install-vipm.sh "${VIPM_DEB_URL}" "${VIPM_DEB_SHA256}"

COPY install-grype.sh /usr/local/sbom/install-grype.sh
RUN bash /usr/local/sbom/install-grype.sh "${GRYPE_VERSION}" "${GRYPE_LINUX_AMD64_SHA256}" /usr/local/bin \
    && grype version

COPY entrypoint.sh /usr/local/sbom/entrypoint.sh
COPY linux_sbom_pipeline.py /usr/local/sbom/linux_sbom_pipeline.py
RUN sed -i 's/\r$//' /usr/local/sbom/entrypoint.sh \
    && chmod +x /usr/local/sbom/entrypoint.sh

ENV DISPLAY=:99 \
    VIPM_NONINTERACTIVE=1 \
    NO_COLOR=1 \
    PYTHONUNBUFFERED=1 \
    OS_UPDATES_APPLIED=${APPLY_OS_UPDATES}

WORKDIR /workspace
ENTRYPOINT ["/usr/local/sbom/entrypoint.sh"]
CMD ["python3", "/usr/local/sbom/linux_sbom_pipeline.py"]
