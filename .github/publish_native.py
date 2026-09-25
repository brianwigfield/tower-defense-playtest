"""Public-side publisher. Never execute or unpack delivered game content."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import zipfile


REPO = "brianwigfield/tower-defense-playtest"
NAMES = (
    "tower-defense-windows-x86_64-unsigned.zip",
    "tower-defense-macos-universal-unsigned.zip",
    "tower-defense-linux-x86_64-unsigned.tar.gz",
)
CHUNK_SIZE = 40 * 1024 * 1024
MAX_ASSET_SIZE = 512 * 1024 * 1024


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_delivery(directory, output, branch):
    manifest_path = directory / "manifest.json"
    require(manifest_path.stat().st_size < 64 * 1024, "Manifest too large")
    manifest = json.loads(manifest_path.read_text())
    require(manifest["version"] == 1, "Unsupported manifest")
    require(re.fullmatch(r"[0-9a-f]{40}", manifest["source_sha"]), "Invalid revision")
    for field in ("run_id", "run_number", "run_attempt"):
        require(type(manifest[field]) is int and 0 < manifest[field] < 10**15,
                f"Invalid {field}")
    require(type(manifest["verify_only"]) is bool, "Invalid verification flag")
    require(branch == f"native-delivery/{manifest['run_id']}-{manifest['run_attempt']}",
            "Manifest does not match delivery branch")
    assets = manifest["assets"]
    require(len(assets) == len(NAMES) and {item["name"] for item in assets} == set(NAMES),
            "Unexpected/missing/duplicate release assets")
    expected_files = {"manifest.json"}
    for asset in assets:
        name = asset["name"]
        require(type(asset["size"]) is int and 1024 <= asset["size"] <= MAX_ASSET_SIZE,
                f"Invalid archive size: {name}")
        chunks = asset["chunks"]
        require(len(chunks) == (asset["size"] + CHUNK_SIZE - 1) // CHUNK_SIZE,
                f"Invalid chunk count: {name}")
        archive = output / name
        with archive.open("wb") as stream:
            for index, chunk in enumerate(chunks):
                part = f"{name}.part{index:03d}"
                require(chunk["name"] == part, "Invalid chunk name/order")
                expected_files.add(part)
                path = directory / part
                expected_size = min(CHUNK_SIZE, asset["size"] - index * CHUNK_SIZE)
                require(not path.is_symlink() and path.stat().st_size == chunk["size"] == expected_size,
                        f"Invalid chunk size/type: {part}")
                require(sha256(path) == chunk["sha256"], f"Chunk checksum mismatch: {part}")
                stream.write(path.read_bytes())
        require(archive.stat().st_size == asset["size"] and sha256(archive) == asset["sha256"],
                f"Archive checksum mismatch: {name}")
        # Check compression integrity without extracting paths or launching binaries.
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as bundle:
                require(sum(info.file_size for info in bundle.infolist()) <= 2 * 1024**3,
                        "Uncompressed ZIP exceeds limit")
                require(bundle.testzip() is None, f"Corrupt ZIP: {name}")
        else:
            with tarfile.open(archive) as bundle:
                total = 0
                for info in bundle:
                    total += info.size
                    require(total <= 2 * 1024**3, "Uncompressed tar exceeds limit")
                    if info.isfile():
                        with bundle.extractfile(info) as stream:
                            while stream.read(1024 * 1024):
                                pass
    require({path.name for path in directory.iterdir()} == expected_files,
            "Unexpected delivery files")
    (output / "SHA256SUMS").write_text(
        "".join(f"{sha256(output / name)}  {name}\n" for name in NAMES), encoding="ascii")
    return manifest


def publish(manifest, output):
    pages = json.loads(command("gh", "api", "--paginate", "--slurp",
                              f"repos/{REPO}/releases?per_page=100"))
    releases = [release for page in pages for release in page]
    sequence = (manifest["run_number"], manifest["run_attempt"])
    for release in releases:
        match = re.search(r"<!-- native-sequence: (\d+)-(\d+) -->", release["body"] or "")
        if not release["draft"] and match and tuple(map(int, match.groups())) > sequence:
            print("A newer native build has already published; skipping stale delivery.")
            return
    tag = f"native-{manifest['run_id']}-{manifest['run_attempt']}"
    existing = next((release for release in releases if release["tag_name"] == tag), None)
    if existing and not existing["draft"]:
        print("This delivery was already published.")
        return
    notes = output / "release-notes.txt"
    notes.write_text(
        "Unsigned development playtests, not production or Steam releases.\n\n"
        "Windows x86_64, Linux x86_64, and universal Intel/Apple Silicon macOS. "
        "Extract the entire archive and keep its files together. "
        "macOS is ad-hoc signed only, not Developer ID signed or notarized; "
        "Gatekeeper and Windows SmartScreen may warn. Only proceed if you trust "
        "this download. Do not disable system protections globally.\n\n"
        "[Download/install help](https://brianwigfield.github.io/tower-defense-playtest/downloads.html)\n\n"
        f"Source revision: `{manifest['source_sha']}`\n\n"
        f"<!-- native-sequence: {sequence[0]}-{sequence[1]} -->\n", encoding="utf-8")
    if not existing:
        existing = json.loads(command(
            "gh", "api", "--method", "POST", f"repos/{REPO}/releases",
            "-f", f"tag_name={tag}", "-f", "target_commitish=main",
            "-F", "draft=true", "-f", "name=Latest playtest",
            "-f", f"body={notes.read_text(encoding='utf-8')}"))
    command("gh", "release", "upload", tag, "--repo", REPO, "--clobber",
            *(str(output / name) for name in (*NAMES, "SHA256SUMS")))
    release = json.loads(command("gh", "api", f"repos/{REPO}/releases/{existing['id']}"))
    expected = {name: (output / name).stat().st_size for name in (*NAMES, "SHA256SUMS")}
    require({asset["name"]: asset["size"] for asset in release["assets"]} == expected,
            "Uploaded release assets do not match the complete package set")
    for asset in release["assets"]:
        require(asset.get("digest") == f"sha256:{sha256(output / asset['name'])}",
                f"Server-side asset checksum mismatch: {asset['name']}")
    command("gh", "release", "edit", tag, "--repo", REPO, "--draft=false", "--latest")
    print(f"Published https://github.com/{REPO}/releases/tag/{tag}")


def main():
    require(os.environ["GITHUB_REPOSITORY"] == REPO, "Wrong repository")
    branch = os.environ["DELIVERY_BRANCH"]
    require(re.fullmatch(r"native-delivery/[0-9]+-[0-9]+", branch), "Invalid delivery branch")
    # Fetch only data with read-only anonymous access. No checkout of delivered code.
    with tempfile.TemporaryDirectory(prefix="native-delivery-") as temp:
        root = Path(temp)
        checkout = root / "delivery-repo"
        command("git", "clone", "--quiet", "--depth", "1", "--single-branch",
                "--branch", branch, f"https://github.com/{REPO}.git", str(checkout))
        delivery = checkout / "delivery"
        tracked = command("git", "-C", str(checkout), "ls-tree", "-r", "HEAD", "--", "delivery")
        require(tracked and all(line.startswith("100644 blob ") for line in tracked.splitlines()),
                "Delivery must contain only regular non-executable files")
        output = root / "packages"
        output.mkdir()
        manifest = read_delivery(delivery, output, branch)
        if manifest["verify_only"]:
            print("Verification-only delivery passed; no release created.")
        else:
            publish(manifest, output)
        command("gh", "api", "--method", "DELETE", f"repos/{REPO}/git/refs/heads/{branch}")
        print("Deleted delivery branch ref (Git objects may remain in repository history).")


if __name__ == "__main__":
    main()
