#!/usr/bin/env python3

import argparse
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "upstreams.json"
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+~-]*$")
ADD_RE = re.compile(
    r"(ADD\s+--checksum=sha256:)([0-9a-f]{64})"
    r"((?:[ \t]*\\\r?\n[ \t]*|[ \t]+))"
    r"(https?://[^\s\\]+)",
    re.MULTILINE,
)


def request_json(url, token):
    headers = {
        "Accept": "application/json",
        "User-Agent": "cpak-upstream-checker",
    }
    if url.startswith("https://api.github.com/"):
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def repository_file(repository, path, token):
    data = request_json(
        f"https://api.github.com/repos/{repository}/contents/{path}?ref=main",
        token,
    )
    import base64

    return base64.b64decode(data["content"]).decode()


def release_tag(settings, token):
    repository = settings["repository"]
    if settings.get("allow_prerelease"):
        releases = request_json(
            f"https://api.github.com/repos/{repository}/releases?per_page=1",
            token,
        )
        if not releases:
            raise RuntimeError(f"{repository} has no published releases")
        tag = releases[0]["tag_name"]
    else:
        release = request_json(
            f"https://api.github.com/repos/{repository}/releases/latest",
            token,
        )
        tag = release["tag_name"]
    prefix = settings.get("strip_prefix", "v")
    suffix = settings.get("strip_suffix", "")
    if prefix and tag.startswith(prefix):
        tag = tag[len(prefix) :]
    if suffix and tag.endswith(suffix):
        tag = tag[: -len(suffix)]
    if not VERSION_RE.fullmatch(tag):
        raise RuntimeError(f"invalid version from {repository}: {tag!r}")
    return tag


def version_key(version):
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"([0-9]+)", version)
    )


def normalize_debian_version(version, settings):
    if settings.get("strip_epoch") and ":" in version:
        version = version.split(":", 1)[1]
    if settings.get("strip_debian_revision") and "-" in version:
        version = version.rsplit("-", 1)[0]
    if not VERSION_RE.fullmatch(version):
        raise RuntimeError(f"invalid Debian package version: {version!r}")
    return version


def debian_package(settings):
    request = urllib.request.Request(
        settings["packages_url"],
        headers={"User-Agent": "cpak-upstream-checker"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
    if settings["packages_url"].endswith(".gz"):
        payload = gzip.decompress(payload)
    records = []
    for block in payload.decode(errors="replace").split("\n\n"):
        record = {}
        for line in block.splitlines():
            if ": " in line:
                key, value = line.split(": ", 1)
                record[key] = value
        if (
            record.get("Package") == settings["package"]
            and record.get("Architecture") in ("amd64", "all")
            and all(key in record for key in ("Version", "Filename", "SHA256", "Size"))
        ):
            records.append(record)
    if not records:
        raise RuntimeError(f"{settings['package']} is missing from the Debian repository")
    record = max(records, key=lambda item: version_key(item["Version"]))
    record["cpak_version"] = normalize_debian_version(record["Version"], settings)
    record["url"] = f"{settings['base_url'].rstrip('/')}/{record['Filename']}"
    return record


def json_release(settings):
    data = request_json(settings["version_url"], None)
    for part in settings["version_path"]:
        data = data[int(part)] if isinstance(data, list) else data[part]
    version = str(data)
    prefix = settings.get("strip_prefix", "")
    suffix = settings.get("strip_suffix", "")
    if prefix and version.startswith(prefix):
        version = version[len(prefix) :]
    if suffix and version.endswith(suffix):
        version = version[: -len(suffix)]
    if not VERSION_RE.fullmatch(version):
        raise RuntimeError(f"invalid version from {settings['version_url']}: {version!r}")
    return {"cpak_version": version}


def pypi_package(settings):
    data = request_json(f"https://pypi.org/pypi/{settings['package']}/json", None)
    candidates = [
        item
        for item in data.get("urls", [])
        if item.get("packagetype") == settings.get("package_type", "bdist_wheel")
        and item.get("python_version") in ("py3", "source")
    ]
    if len(candidates) != 1:
        raise RuntimeError(f"PyPI returned {len(candidates)} matching files for {settings['package']}")
    artifact = candidates[0]
    version = data["info"]["version"]
    if not VERSION_RE.fullmatch(version):
        raise RuntimeError(f"invalid PyPI version for {settings['package']}: {version!r}")
    return {
        "cpak_version": version,
        "Filename": artifact["filename"],
        "SHA256": artifact["digests"]["sha256"],
        "Size": str(artifact["size"]),
        "url": artifact["url"],
    }


def direct_debian_package(settings, manifest):
    sources = manifest.get("runtime_sources", [])
    if len(sources) != 1:
        raise RuntimeError("direct Debian sources require exactly one runtime source")
    current = sources[0]
    request = urllib.request.Request(
        settings["latest_url"],
        headers={"User-Agent": "cpak-upstream-checker"},
        method="HEAD",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        final_url = response.geturl()
        content_length = response.headers.get("Content-Length")
    if (
        content_length
        and int(content_length) == current["size"]
        and (final_url == current["url"] or settings["latest_url"] == current["url"])
    ):
        return {
            "cpak_version": manifest["version"],
            "Filename": current["name"],
            "SHA256": current["sha256"],
            "Size": str(current["size"]),
            "url": current["url"],
        }

    digest = hashlib.sha256()
    size = 0
    with tempfile.NamedTemporaryFile(suffix=".deb") as artifact:
        request = urllib.request.Request(
            final_url,
            headers={"User-Agent": "cpak-upstream-checker"},
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            while chunk := response.read(1024 * 1024):
                artifact.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        artifact.flush()
        version = subprocess.check_output(
            ["dpkg-deb", "--field", artifact.name, "Version"],
            text=True,
        ).strip()
    url = settings["latest_url"] if settings.get("keep_latest_url") else final_url
    filename = current["name"] if settings.get("keep_name") else url.rsplit("/", 1)[-1]
    return {
        "cpak_version": normalize_debian_version(version, settings),
        "Filename": filename,
        "SHA256": digest.hexdigest(),
        "Size": str(size),
        "url": url,
    }


def upstream(settings, token, manifest=None):
    if settings.get("kind", "github-release") == "debian-repository":
        return debian_package(settings)
    if settings.get("kind") == "direct-deb":
        return direct_debian_package(settings, manifest)
    if settings.get("kind") == "json-release":
        return json_release(settings)
    if settings.get("kind") == "pypi":
        return pypi_package(settings)
    return {"cpak_version": release_tag(settings, token)}


def package_manifest(repository, token):
    manifest = json.loads(repository_file(repository, "cpak.json", token))
    version = manifest.get("version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise RuntimeError(f"invalid package version in {repository}")
    return manifest


def checked_urls(containerfile):
    return [match.group(4) for match in ADD_RE.finditer(containerfile)]


def source_available(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "cpak-upstream-checker"},
        method="HEAD",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status < 400
    except urllib.error.HTTPError as error:
        if error.code in (403, 405, 406):
            request = urllib.request.Request(
                url,
                headers={"Range": "bytes=0-0", "User-Agent": "cpak-upstream-checker"},
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.status < 400
            except urllib.error.HTTPError as fallback_error:
                if fallback_error.code in (403, 404, 406):
                    return False
                raise
        if error.code == 404:
            return False
        raise


def plan(token):
    packages = json.loads(CONFIG.read_text())
    def check(package):
        name, settings = package
        repository = f"Containerpak/{name}"
        manifest = package_manifest(repository, token)
        current = manifest["version"]
        source = upstream(settings, token, manifest)
        latest = source["cpak_version"]
        if current != latest:
            if version_key(latest) <= version_key(current):
                return None
            if settings.get("kind") in ("debian-repository", "direct-deb", "pypi"):
                return {"name": name, "version": latest}
            runtime_sources = manifest.get("runtime_sources", [])
            if len(runtime_sources) == 1 and current in runtime_sources[0]["url"]:
                latest_url = runtime_sources[0]["url"].replace(current, latest)
                if source_available(latest_url):
                    return {"name": name, "version": latest}
                return None
            containerfile = repository_file(repository, "Containerfile", token)
            current_urls = checked_urls(containerfile)
            latest_urls = checked_urls(containerfile.replace(current, latest))
            if settings.get("unchecked") and current in containerfile and current_urls == latest_urls:
                return {"name": name, "version": latest}
            if current_urls == latest_urls:
                raise RuntimeError(f"{name}: release URL does not contain {current}")
            if not latest_urls or not all(source_available(url) for url in latest_urls):
                return None
            return {"name": name, "version": latest}

    with ThreadPoolExecutor(max_workers=8) as executor:
        updates = [update for update in executor.map(check, packages.items()) if update]
    return {"include": updates}


def file_metadata(url):
    request = urllib.request.Request(url, headers={"User-Agent": "cpak-upstream-checker"})
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=120) as response:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def file_hash(url):
    return file_metadata(url)[0]


def replace_json_field(text, package, key, old, new):
    pattern = re.compile(
        rf'("{re.escape(key)}"\s*:\s*){re.escape(json.dumps(old))}'
    )
    updated, count = pattern.subn(
        lambda match: match.group(1) + json.dumps(new),
        text,
        count=1,
    )
    if count != 1:
        raise RuntimeError(f"{package}: expected one {key} field with value {old!r}")
    return updated


def update_package(path, name, version):
    packages = json.loads(CONFIG.read_text())
    settings = packages[name]
    manifest_path = path / "cpak.json"
    containerfile_path = path / "Containerfile"
    manifest_text = manifest_path.read_text()
    manifest = json.loads(manifest_text)
    current = manifest["version"]
    source = upstream(settings, os.environ.get("GH_TOKEN"), manifest)
    expected = source["cpak_version"]
    if version != expected:
        raise RuntimeError(f"release changed while updating {name}: {version} != {expected}")
    if current == version:
        return False

    if settings.get("kind") in ("debian-repository", "direct-deb", "pypi"):
        sources = manifest.get("runtime_sources", [])
        if len(sources) == 1:
            runtime_source = sources[0]
            values = {
                "version": (current, version),
                "name": (runtime_source["name"], source["Filename"].rsplit("/", 1)[-1]),
                "url": (runtime_source["url"], source["url"]),
                "sha256": (runtime_source["sha256"], source["SHA256"]),
                "size": (runtime_source["size"], int(source["Size"])),
            }
            for key, (old, new) in values.items():
                manifest_text = replace_json_field(manifest_text, name, key, old, new)
            json.loads(manifest_text)
            manifest_path.write_text(manifest_text)
            return True
        if settings.get("kind") == "direct-deb":
            raise RuntimeError(f"{name}: expected exactly one runtime source")

        containerfile = containerfile_path.read_text()
        updated = containerfile.replace(current, version)
        matches = list(ADD_RE.finditer(updated))
        if settings.get("kind") == "pypi" and len(matches) == 1:
            match = matches[0]
            replacement = match.group(1) + source["SHA256"] + match.group(3) + source["url"]
            updated = updated[: match.start()] + replacement + updated[match.end() :]
            selected = []
        else:
            selected = [match for match in matches if match.group(4) == source["url"]]
        if len(selected) != 1:
            if settings.get("kind") != "pypi":
                raise RuntimeError(f"{name}: package URL does not match the checked source")
        else:
            match = selected[0]
            updated = updated[: match.start(2)] + source["SHA256"] + updated[match.end(2) :]
        version_pattern = re.compile(
            rf'("version"\s*:\s*"){re.escape(current)}(")',
            re.MULTILINE,
        )
        manifest_text, count = version_pattern.subn(rf"\g<1>{version}\2", manifest_text, count=1)
        if count != 1:
            raise RuntimeError(f"{name}: manifest version was not updated")
        json.loads(manifest_text)
        containerfile_path.write_text(updated)
        manifest_path.write_text(manifest_text)
        return True

    sources = manifest.get("runtime_sources", [])
    if len(sources) == 1 and current in sources[0]["url"]:
        runtime_source = sources[0]
        url = runtime_source["url"].replace(current, version)
        digest, size = file_metadata(url)
        values = {
            "version": (current, version),
            "name": (runtime_source["name"], runtime_source["name"].replace(current, version)),
            "url": (runtime_source["url"], url),
            "sha256": (runtime_source["sha256"], digest),
            "size": (runtime_source["size"], size),
        }
        for key, (old, new) in values.items():
            manifest_text = replace_json_field(manifest_text, name, key, old, new)
        json.loads(manifest_text)
        manifest_path.write_text(manifest_text)
        return True

    containerfile = containerfile_path.read_text()
    if current not in containerfile:
        raise RuntimeError(f"{name}: {current} is not present in Containerfile")
    updated = containerfile.replace(current, version)
    if updated == containerfile:
        raise RuntimeError(f"{name}: Containerfile did not change")

    original_urls = checked_urls(containerfile)
    updated_matches = list(ADD_RE.finditer(updated))
    if len(original_urls) != len(updated_matches):
        raise RuntimeError(f"{name}: checksum source count changed")

    replacements = []
    for old_url, match in zip(original_urls, updated_matches):
        new_url = match.group(4)
        if old_url == new_url:
            continue
        replacements.append((match.start(2), match.end(2), file_hash(new_url)))
    if not replacements and not settings.get("unchecked"):
        raise RuntimeError(f"{name}: no checked source URL changed")
    for start, end, digest in reversed(replacements):
        updated = updated[:start] + digest + updated[end:]

    version_pattern = re.compile(
        rf'("version"\s*:\s*"){re.escape(current)}(")',
        re.MULTILINE,
    )
    manifest_text, count = version_pattern.subn(rf"\g<1>{version}\2", manifest_text, count=1)
    if count != 1:
        raise RuntimeError(f"{name}: manifest version was not updated")
    json.loads(manifest_text)
    containerfile_path.write_text(updated)
    manifest_path.write_text(manifest_text)
    return True


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("plan")
    update = subparsers.add_parser("update")
    update.add_argument("--path", type=Path, required=True)
    update.add_argument("--name", required=True)
    update.add_argument("--version", required=True)
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN")
    if args.command == "plan":
        print(json.dumps(plan(token), separators=(",", ":")))
        return
    changed = update_package(args.path, args.name, args.version)
    print("changed" if changed else "current")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(error, file=sys.stderr)
        sys.exit(1)
