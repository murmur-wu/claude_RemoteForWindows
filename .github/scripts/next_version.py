from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass


SEMVER_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
BREAKING_RE = re.compile(r"^[^\s:(]+(?:\([^\n]+\))?!:", re.MULTILINE)
FEATURE_RE = re.compile(r"^feat(?:\([^\n]+\))?:", re.MULTILINE)


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int

    def bump(self, bump_type: str) -> "Version":
        if bump_type == "major":
            return Version(self.major + 1, 0, 0)
        if bump_type == "minor":
            return Version(self.major, self.minor + 1, 0)
        return Version(self.major, self.minor, self.patch + 1)

    def tag(self) -> str:
        return f"v{self.major}.{self.minor}.{self.patch}"


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        check=check,
        capture_output=True,
    )
    return result.stdout.decode("utf-8", errors="replace").strip()


def latest_tag() -> str | None:
    tags = git("tag", "--list", "v*", "--sort=-v:refname")
    for tag in tags.splitlines():
        if SEMVER_TAG_RE.match(tag):
            return tag
    return None


def parse_version(tag: str | None) -> Version:
    if not tag:
        return Version(0, 0, 0)
    match = SEMVER_TAG_RE.match(tag)
    if not match:
        raise ValueError(f"Unsupported tag format: {tag}")
    return Version(*(int(part) for part in match.groups()))


def tags_on_head() -> list[str]:
    tags = git("tag", "--points-at", "HEAD")
    return [tag for tag in tags.splitlines() if SEMVER_TAG_RE.match(tag)]


def commit_messages(previous_tag: str | None) -> str:
    if previous_tag:
        return git("log", f"{previous_tag}..HEAD", "--format=%s%n%b")
    return git("log", "--format=%s%n%b")


def decide_bump(messages: str) -> str:
    if not messages.strip():
        return "patch"
    if "BREAKING CHANGE" in messages or BREAKING_RE.search(messages):
        return "major"
    if FEATURE_RE.search(messages):
        return "minor"
    return "patch"


def emit(**values: str) -> None:
    for key, value in values.items():
        print(f"{key}={value}")


def main() -> int:
    head_tags = sorted(tags_on_head(), reverse=True)
    if head_tags:
        emit(
            should_release="false",
            tag=head_tags[0],
            version=head_tags[0].removeprefix("v"),
            bump="none",
            previous_tag=head_tags[0],
        )
        return 0

    previous_tag = latest_tag()
    messages = commit_messages(previous_tag)
    bump = decide_bump(messages)
    next_version = parse_version(previous_tag).bump(bump)

    emit(
        should_release="true",
        tag=next_version.tag(),
        version=next_version.tag().removeprefix("v"),
        bump=bump,
        previous_tag=previous_tag or "",
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        if error.stderr:
            print(error.stderr.decode("utf-8", errors="replace"), file=sys.stderr)
        raise SystemExit(error.returncode)