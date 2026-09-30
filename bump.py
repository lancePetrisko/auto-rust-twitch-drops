"""Bump the app version, commit it, and tag the release.

Usage:
    python bump.py patch   # 0.1.0 -> 0.1.1  (bug fixes, small tweaks)
    python bump.py minor   # 0.1.0 -> 0.2.0  (new features)
    python bump.py major   # 0.1.0 -> 1.0.0  (big or breaking changes)

Only the VERSION file is committed, so commit your actual changes first.
"""
import os
import subprocess
import sys

VERSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")
PARTS = ("major", "minor", "patch")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in PARTS:
        sys.exit(__doc__)

    with open(VERSION_PATH, "r", encoding="utf-8") as f:
        numbers = [int(n) for n in f.read().strip().split(".")]

    index = PARTS.index(sys.argv[1])
    numbers[index] += 1
    numbers[index + 1:] = [0] * (2 - index)
    new_version = ".".join(str(n) for n in numbers)

    with open(VERSION_PATH, "w", encoding="utf-8") as f:
        f.write(new_version + "\n")

    tag = f"v{new_version}"
    subprocess.run(["git", "add", VERSION_PATH], check=True)
    subprocess.run(["git", "commit", "-m", f"Release {tag}", "--", VERSION_PATH], check=True)
    subprocess.run(["git", "tag", "-a", tag, "-m", f"Release {tag}"], check=True)
    print(f"Bumped to {tag}. Push with: git push && git push origin {tag}")


if __name__ == "__main__":
    main()
