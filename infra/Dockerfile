# syntax=docker/dockerfile:1
#
# Two stages. The first needs a compiler; the second must not have one.
#
# The single-stage version of this file shipped `build-essential` into the
# runtime image, which is both a large CVE surface and a gift to anyone who
# gets code execution inside the container. It also kept `pip` and
# `setuptools` around, which nothing at runtime uses. Trivy found 42 Debian
# CVEs and 4 Python ones; every single one was in the base image rather than
# in this project's dependencies.

# --------------------------------------------------------------- builder ---
FROM python:3.12.10-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Compilers live here and go no further. psycopg[binary] and pydantic-core
# ship wheels, but a dependency without one must still be buildable rather
# than failing the build on a machine that happens to lack a wheel.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

# A virtualenv rather than the system site-packages, so the runtime stage can
# copy one self-contained directory and nothing else.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

# setuptools is upgraded in the builder because the base image pins 70.3.0,
# which carries CVE-2025-47273. It is deleted from the runtime stage anyway;
# this keeps the *build* from running a vulnerable one.
RUN pip install --upgrade pip setuptools wheel \
 && pip install .

# --------------------------------------------------------------- runtime ---
FROM python:3.12.10-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# `apt-get upgrade` is deliberate, and it is a trade.
#
# The base tag is pinned for reproducibility, which also means its Debian
# packages are frozen at whenever that tag was built. Everything Trivy flagged
# -- openssl, gnutls, krb5, pam, pcre2, sqlite -- has a fix published in a
# Debian security update that the pinned tag predates. Not applying them means
# shipping known-exploitable TLS and Kerberos libraries in a service that
# handles PHI.
#
# The cost is that two builds of the same commit can differ. That is the right
# way round: a byte-identical image is worth less than one without a
# remote-code-execution path in its TLS stack, and the reproducibility that
# matters for audit comes from the lockfile and the image digest recorded at
# release, not from the base layer never changing.
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends libpq5 \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# Remove the package manager from the image it is not meant to change.
#
# Nothing at runtime imports pip or setuptools, and both carry vulnerabilities
# of their own: pip vendors its own copies of urllib3 and msgpack, which Trivy
# reports separately and which no dependency pin can reach. Deleting them also
# removes the ability to install anything into a running container, which is a
# property worth having on purpose rather than by accident.
#
# Verified before adopting: the application imports and serves with all of
# these gone.
RUN set -eux; \
    for site in /usr/local/lib/python3.12/site-packages /opt/venv/lib/python3.12/site-packages; do \
        rm -rf "$site"/pip "$site"/pip-*.dist-info \
               "$site"/setuptools "$site"/setuptools-*.dist-info \
               "$site"/pkg_resources \
               "$site"/wheel "$site"/wheel-*.dist-info \
               "$site"/_distutils_hack "$site"/distutils-precedence.pth; \
    done; \
    rm -f /usr/local/bin/pip* /opt/venv/bin/pip* /usr/local/bin/wheel /opt/venv/bin/wheel; \
    python -c "import rcm.api.main; print('runtime import OK')"

# Runs unprivileged: a container that can write its own code is a container an
# attacker can rewrite.
RUN useradd --create-home --uid 10001 rcm \
 && mkdir -p /app \
 && chown rcm:rcm /app
WORKDIR /app
USER rcm

# /opt/venv stays root-owned on purpose: the service reads its own code and
# never writes it, so the application user cannot modify what it executes.

EXPOSE 8000
CMD ["uvicorn", "rcm.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
