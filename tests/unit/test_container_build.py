"""The image's security-upgrade layer must actually re-run (S-2, container scan).

`infra/Dockerfile` runs `apt-get upgrade -y` in the runtime stage, and its
comment accepts a loss of byte-for-byte reproducibility in exchange for
shipping current security patches. That trade only happens if the layer is
rebuilt.

It was not. CI caches layers (`cache-from: type=gha`), and a `RUN` whose text
and parent layer are unchanged is restored rather than re-executed, so the
upgrade ran once and was replayed on every build afterwards. Caching had
quietly handed back the reproducibility the comment gives up, and taken the
patching with it. The symptom was Trivy failing on CVE-2026-84782 -- an
OpenSSL information disclosure fixed in Debian's `3.0.22-1~deb12u2` -- on a
pull request that changed nothing in that file.

The repair has two halves, in two files, and each is useless alone: the
Dockerfile takes a `SECURITY_REFRESH` build argument into the layer's cache
key, and CI passes the current date. Remove either and the layer silently
caches forever again -- the argument has a default, so nothing fails, which
is exactly how this is lost a second time.

These tests cannot build an image. They check that both halves are present
and refer to each other, which is the part that drifts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "infra" / "Dockerfile"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

SECURITY_ARG = "SECURITY_REFRESH"


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def runtime_stage(dockerfile: str) -> str:
    """Everything from `FROM ... AS runtime` to the end.

    The builder stage also runs apt, and its packages never reach the
    shipped image, so asserting against the whole file would pass on the
    wrong stage.
    """
    match = re.search(r"^FROM .*AS runtime\b", dockerfile, re.MULTILINE)
    assert match, "no runtime stage in the Dockerfile"
    return dockerfile[match.start() :]


@pytest.fixture(scope="module")
def build_step() -> dict[str, object]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in workflow["jobs"]["build"]["steps"]:
        uses = str(step.get("uses", ""))
        if uses.startswith("docker/build-push-action"):
            return dict(step.get("with", {}))
    pytest.fail("the build job no longer uses docker/build-push-action")


# ---------------------------------------------------------------------------
# The two halves
# ---------------------------------------------------------------------------


def test_the_runtime_stage_still_applies_security_updates(runtime_stage: str) -> None:
    """The control itself. Without it the image ships whatever Debian
    packages the pinned base tag froze, in a service that handles PHI."""
    assert "apt-get upgrade -y" in runtime_stage


def test_the_security_layer_takes_a_refresh_argument(runtime_stage: str) -> None:
    """Declared *and* referenced inside the `RUN`.

    An `ARG` the command does not mention is not part of the layer's cache
    key, so the layer would be restored from cache whatever value CI passes
    -- the repair would look present and do nothing.
    """
    assert f"ARG {SECURITY_ARG}" in runtime_stage

    upgrade_run = re.search(
        r"^RUN (?:.|\n)*?apt-get upgrade -y(?:.|\n)*?(?=\n(?:[A-Z]{2,}|#)|\Z)",
        runtime_stage,
        re.MULTILINE,
    )
    assert upgrade_run, "could not find the apt-get upgrade RUN"
    assert f"${{{SECURITY_ARG}}}" in upgrade_run.group(0), (
        f"the upgrade layer does not reference ${SECURITY_ARG}, so the build "
        "argument is not in its cache key and the layer will be restored from "
        "cache regardless"
    )


@pytest.mark.invariant
def test_ci_passes_a_refresh_key_to_the_build(build_step: dict[str, object]) -> None:
    """The other half. The Dockerfile's argument has a default, so a build
    that stops passing this fails nothing and caches forever."""
    build_args = str(build_step.get("build-args", ""))
    assert f"{SECURITY_ARG}=" in build_args, (
        "the build step passes no SECURITY_REFRESH, so the security-upgrade "
        "layer will be served from cache on every build"
    )


def test_the_refresh_key_changes_over_time(build_step: dict[str, object]) -> None:
    """A constant would be worse than nothing: it looks like a cache-bust
    and never busts anything.

    The value is wired to a step output rather than a literal, and the step
    that produces it computes a date.
    """
    build_args = str(build_step.get("build-args", ""))
    reference = re.search(rf"{SECURITY_ARG}=\$\{{\{{\s*steps\.(\w+)\.outputs", build_args)
    assert reference, f"{SECURITY_ARG} is set to a literal, which never changes"

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    producer = next(
        step for step in workflow["jobs"]["build"]["steps"] if step.get("id") == reference.group(1)
    )
    assert "date" in str(producer.get("run", "")), (
        "the step feeding SECURITY_REFRESH does not compute a date, so the "
        "key may not change between builds"
    )


# ---------------------------------------------------------------------------
# Properties the Dockerfile's comments claim
# ---------------------------------------------------------------------------


def test_the_runtime_stage_ships_no_compiler(runtime_stage: str) -> None:
    """*"The first needs a compiler; the second must not have one."*

    `build-essential` in the shipped image is both a large CVE surface and
    a gift to anyone who gets code execution inside the container.
    """
    assert "build-essential" not in runtime_stage


def test_the_runtime_stage_removes_the_package_manager(runtime_stage: str) -> None:
    """Nothing at runtime imports pip, and it vendors its own urllib3 and
    msgpack that no dependency pin can reach. Removing it also removes the
    ability to install anything into a running container."""
    assert "rm -rf" in runtime_stage
    assert '"$site"/pip' in runtime_stage


def test_the_image_runs_unprivileged(runtime_stage: str) -> None:
    """A container that can write its own code is a container an attacker
    can rewrite."""
    assert re.search(r"^USER rcm$", runtime_stage, re.MULTILINE)


def test_the_base_image_is_pinned(dockerfile: str) -> None:
    """Both stages pin an exact patch version. `python:3.12-slim` would make
    the Python version drift silently between builds, which is the
    reproducibility that does matter -- as distinct from the Debian package
    set, which is refreshed on purpose."""
    bases = re.findall(r"^FROM (\S+)", dockerfile, re.MULTILINE)
    assert bases, "no FROM lines"
    for base in bases:
        assert re.search(r"python:3\.\d+\.\d+-slim", base), f"{base} is not pinned to a patch"
