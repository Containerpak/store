#!/usr/bin/env python3

import argparse
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
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
PACKAGE_SOURCE_KINDS = {
    "android-studio",
    "debian-repository",
    "direct-archive",
    "direct-deb",
    "direct-deb-add",
    "html-asset",
    "pypi",
}
ARGUMENT_SOURCE_KINDS = {
    "debian-archive",
    "github-release-asset",
    "github-tag",
    "go-release",
    "kde-source",
    "node-lts",
    "zig-stable",
}


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


def request_text(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "cpak-upstream-checker"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
        if payload.startswith(b"\x1f\x8b"):
            payload = gzip.decompress(payload)
        return payload.decode(errors="replace")


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


def android_studio_package(settings):
    request = urllib.request.Request(
        settings["page_url"],
        headers={"User-Agent": "cpak-upstream-checker"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        page = response.read().decode(errors="replace")
    link = re.search(
        r'href="(?P<url>https://[^" ]+/android/studio/ide-zips/'
        r'(?P<version>[0-9.]+)/(?P<name>android-studio-[^"/]+-linux\.tar\.gz))"',
        page,
    )
    if not link:
        raise RuntimeError("Android Studio Linux archive is missing from the download page")
    table = re.search(
        rf'>{re.escape(link.group("name"))}</button>\s*</td>\s*<td>[^<]+</td>\s*'
        r'<td>(?P<sha256>[0-9a-f]{64})</td>',
        page,
    )
    if not table:
        raise RuntimeError("Android Studio checksum is missing from the download page")
    request = urllib.request.Request(
        link.group("url"),
        headers={"User-Agent": "cpak-upstream-checker"},
        method="HEAD",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        size = response.headers.get("Content-Length")
    if not size:
        raise RuntimeError("Android Studio archive has no Content-Length")
    return {
        "cpak_version": link.group("version"),
        "Filename": link.group("name"),
        "SHA256": table.group("sha256"),
        "Size": size,
        "url": link.group("url"),
    }


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


def github_release_asset(settings, token):
    repository = settings["repository"]
    tag_prefix = settings.get("tag_prefix")
    if tag_prefix:
        releases = request_json(
            f"https://api.github.com/repos/{repository}/releases?per_page=20",
            token,
        )
        releases = [item for item in releases if item["tag_name"].startswith(tag_prefix)]
        if not releases:
            raise RuntimeError(f"{repository} has no release starting with {tag_prefix!r}")
        release = releases[0]
    else:
        release = request_json(
            f"https://api.github.com/repos/{repository}/releases/latest",
            token,
        )
    tag = release["tag_name"]
    prefix = settings.get("strip_prefix", tag_prefix or "v")
    version = tag[len(prefix) :] if prefix and tag.startswith(prefix) else tag
    if not VERSION_RE.fullmatch(version):
        raise RuntimeError(f"invalid release version from {repository}: {version!r}")
    pattern = re.compile(settings["asset_pattern"].format(version=re.escape(version)))
    assets = [asset for asset in release.get("assets", []) if pattern.fullmatch(asset["name"])]
    if len(assets) != 1:
        raise RuntimeError(f"{repository} has {len(assets)} matching release assets")
    asset = assets[0]
    digest = asset.get("digest") or ""
    if digest.startswith("sha256:"):
        digest = digest.split(":", 1)[1]
    else:
        digest = file_hash(asset["browser_download_url"])
    return {
        "cpak_version": version,
        "Filename": asset["name"],
        "SHA256": digest,
        "Size": str(asset["size"]),
        "url": asset["browser_download_url"],
    }


def github_tag(settings, token):
    repository = settings["repository"]
    if settings.get("latest_release"):
        release = request_json(
            f"https://api.github.com/repos/{repository}/releases/latest",
            token,
        )
        tag_name = release["tag_name"]
        ref = request_json(
            "https://api.github.com/repos/"
            f"{repository}/git/ref/tags/{urllib.parse.quote(tag_name, safe='')}",
            token,
        )["object"]
        if ref["type"] == "tag":
            ref = request_json(
                f"https://api.github.com/repos/{repository}/git/tags/{ref['sha']}",
                token,
            )["object"]
        commit = ref["sha"]
    else:
        tags = request_json(
            f"https://api.github.com/repos/{repository}/tags?per_page=1",
            token,
        )
        if not tags:
            raise RuntimeError(f"{repository} has no tags")
        tag_name = tags[0]["name"]
        commit = tags[0]["commit"]["sha"]
    prefix = settings.get("strip_prefix", "v")
    version = tag_name[len(prefix) :] if prefix and tag_name.startswith(prefix) else tag_name
    if not VERSION_RE.fullmatch(version):
        raise RuntimeError(f"invalid tag version from {repository}: {version!r}")
    source = {"cpak_version": version, "commit": commit}
    if settings.get("source_url_template"):
        source["url"] = settings["source_url_template"].format(
            commit=commit,
            tag=tag_name,
            version=version,
        )
        source["SHA256"] = file_hash(source["url"])
    return source


def go_release():
    releases = request_json("https://go.dev/dl/?mode=json", None)
    if not releases:
        raise RuntimeError("Go returned no stable releases")
    release = releases[0]
    version = release["version"].removeprefix("go")
    files = {
        item["arch"]: item
        for item in release["files"]
        if item["os"] == "linux" and item["arch"] in ("amd64", "arm64")
    }
    if set(files) != {"amd64", "arm64"}:
        raise RuntimeError("Go is missing a Linux archive")
    return {
        "cpak_version": version,
        "sha256_amd64": files["amd64"]["sha256"],
        "sha256_arm64": files["arm64"]["sha256"],
    }


def node_lts():
    releases = request_json("https://nodejs.org/dist/index.json", None)
    release = next((item for item in releases if item.get("lts")), None)
    if not release:
        raise RuntimeError("Node.js returned no LTS release")
    tag = release["version"]
    version = tag.removeprefix("v")
    checksums = {}
    for line in request_text(f"https://nodejs.org/dist/{tag}/SHASUMS256.txt").splitlines():
        if "  " in line:
            digest, name = line.split("  ", 1)
            checksums[name] = digest
    try:
        amd64 = checksums[f"node-{tag}-linux-x64.tar.xz"]
        arm64 = checksums[f"node-{tag}-linux-arm64.tar.xz"]
    except KeyError as error:
        raise RuntimeError(f"Node.js is missing {error.args[0]}") from error
    return {
        "cpak_version": version,
        "sha256_amd64": amd64,
        "sha256_arm64": arm64,
    }


def zig_stable():
    releases = request_json("https://ziglang.org/download/index.json", None)
    versions = [
        key
        for key in releases
        if key[:1].isdigit() and VERSION_RE.fullmatch(key) and "dev" not in key
    ]
    if not versions:
        raise RuntimeError("Zig returned no stable release")
    version = max(versions, key=version_key)
    source = releases[version]["x86_64-linux"]
    return {
        "cpak_version": version,
        "SHA256": source["shasum"],
        "url": source["tarball"],
    }


def kde_source(settings):
    page = request_text(settings["page_url"])
    versions = re.findall(r'href="([0-9]+\.[0-9]+\.[0-9]+)/', page)
    if not versions:
        raise RuntimeError("KDE release service returned no releases")
    version = max(set(versions), key=version_key)
    url = settings["source_url_template"].format(version=version)
    checksum = request_text(f"{url}.sha256").split()[0]
    if not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise RuntimeError("KDE release service returned an invalid checksum")
    return {"cpak_version": version, "SHA256": checksum, "url": url}


def html_asset(settings):
    page = request_text(settings["page_url"])
    match = re.search(settings["link_pattern"], page)
    if not match:
        raise RuntimeError(f"no matching asset on {settings['page_url']}")
    version = match.group("version")
    url = match.group("url")
    checksum = request_text(url + settings["checksum_suffix"]).split()[0]
    if not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise RuntimeError(f"invalid checksum for {url}")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "cpak-upstream-checker"},
        method="HEAD",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        size = response.headers.get("Content-Length")
    return {
        "cpak_version": version,
        "Filename": url.rsplit("/", 1)[-1],
        "SHA256": checksum,
        "Size": size or "0",
        "url": url,
    }


def debian_archive(settings, manifest):
    source = debian_package(settings)
    version = source["cpak_version"]
    result = {"cpak_version": version}
    if manifest and manifest.get("version") == version:
        return result
    url = settings["source_url_template"].format(version=version)
    digest, size = file_metadata(url)
    result.update({"url": url, "SHA256": digest, "Size": str(size)})
    return result


def gitlab_tag(settings):
    tags = request_json(settings["tags_url"], None)
    if not tags:
        raise RuntimeError(f"GitLab returned no tags from {settings['tags_url']}")
    tag = tags[0]
    version = tag["name"]
    prefix = settings.get("strip_prefix", "")
    if prefix and version.startswith(prefix):
        version = version[len(prefix) :]
    if not VERSION_RE.fullmatch(version):
        raise RuntimeError(f"invalid GitLab version: {version!r}")
    commit = tag["commit"]["id"]
    return {
        "cpak_version": version,
        "source_url": settings["source_url_template"].format(
            commit=commit,
            tag=tag["name"],
            version=version,
        ),
    }


def debian_artifact(url):
    digest = hashlib.sha256()
    size = 0
    with tempfile.NamedTemporaryFile(suffix=".deb") as artifact:
        request = urllib.request.Request(
            url,
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
    return version, digest.hexdigest(), size


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

    version, digest, size = debian_artifact(final_url)
    url = settings["latest_url"] if settings.get("keep_latest_url") else final_url
    filename = current["name"] if settings.get("keep_name") else url.rsplit("/", 1)[-1]
    return {
        "cpak_version": normalize_debian_version(version, settings),
        "Filename": filename,
        "SHA256": digest,
        "Size": str(size),
        "url": url,
    }


def direct_debian_add(settings, manifest, containerfile):
    matches = list(ADD_RE.finditer(containerfile or ""))
    if len(matches) != 1:
        raise RuntimeError("direct Debian ADD sources require exactly one checked source")
    current = matches[0]
    request = urllib.request.Request(
        settings["latest_url"],
        headers={"User-Agent": "cpak-upstream-checker"},
        method="HEAD",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        final_url = response.geturl()
    if final_url == current.group(4):
        return {
            "cpak_version": manifest["version"],
            "Filename": final_url.rsplit("/", 1)[-1],
            "SHA256": current.group(2),
            "url": final_url,
        }
    version, digest, size = debian_artifact(final_url)
    return {
        "cpak_version": normalize_debian_version(version, settings),
        "Filename": final_url.rsplit("/", 1)[-1],
        "SHA256": digest,
        "Size": str(size),
        "url": final_url,
    }


def direct_archive_package(settings, manifest):
    sources = manifest.get("runtime_sources", [])
    if len(sources) != 1:
        raise RuntimeError("direct archive sources require exactly one runtime source")
    current = sources[0]
    request = urllib.request.Request(
        settings["latest_url"],
        headers={"User-Agent": "cpak-upstream-checker"},
        method="HEAD",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        final_url = response.geturl()
        content_length = response.headers.get("Content-Length")
    if content_length and int(content_length) == current["size"] and final_url == current["url"]:
        return {
            "cpak_version": manifest["version"],
            "Filename": current["name"],
            "SHA256": current["sha256"],
            "Size": str(current["size"]),
            "url": current["url"],
        }

    digest = hashlib.sha256()
    size = 0
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as artifact:
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
        with tarfile.open(artifact.name, "r:*") as archive:
            suffix = settings["version_member"]
            members = [member for member in archive.getmembers() if member.name.endswith(suffix)]
            if len(members) != 1:
                raise RuntimeError(f"archive contains {len(members)} files ending with {suffix!r}")
            stream = archive.extractfile(members[0])
            if stream is None:
                raise RuntimeError(f"cannot read {members[0].name} from archive")
            data = json.load(stream)
    version = data
    for part in settings.get("version_path", ["version"]):
        version = version[int(part)] if isinstance(version, list) else version[part]
    version = str(version)
    if not VERSION_RE.fullmatch(version):
        raise RuntimeError(f"invalid archive version: {version!r}")
    filename = settings.get("name_template", current["name"]).format(version=version)
    return {
        "cpak_version": version,
        "Filename": filename,
        "SHA256": digest.hexdigest(),
        "Size": str(size),
        "url": final_url,
    }


def upstream(settings, token, manifest=None, containerfile=None):
    if settings.get("kind") == "android-studio":
        return android_studio_package(settings)
    if settings.get("kind", "github-release") == "debian-repository":
        return debian_package(settings)
    if settings.get("kind") == "direct-deb":
        return direct_debian_package(settings, manifest)
    if settings.get("kind") == "direct-deb-add":
        return direct_debian_add(settings, manifest, containerfile)
    if settings.get("kind") == "direct-archive":
        return direct_archive_package(settings, manifest)
    if settings.get("kind") == "json-release":
        return json_release(settings)
    if settings.get("kind") == "pypi":
        return pypi_package(settings)
    if settings.get("kind") == "gitlab-tag":
        return gitlab_tag(settings)
    if settings.get("kind") == "github-release-asset":
        return github_release_asset(settings, token)
    if settings.get("kind") == "github-tag":
        return github_tag(settings, token)
    if settings.get("kind") == "go-release":
        return go_release()
    if settings.get("kind") == "node-lts":
        return node_lts()
    if settings.get("kind") == "zig-stable":
        return zig_stable()
    if settings.get("kind") == "kde-source":
        return kde_source(settings)
    if settings.get("kind") == "html-asset":
        return html_asset(settings)
    if settings.get("kind") == "debian-archive":
        return debian_archive(settings, manifest)
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
        containerfile = None
        if settings.get("kind") in ("direct-deb-add", "html-asset"):
            containerfile = repository_file(repository, "Containerfile", token)
        source = upstream(settings, token, manifest, containerfile)
        latest = source["cpak_version"]
        if current != latest:
            if version_key(latest) <= version_key(current):
                return None
            if settings.get("kind") in PACKAGE_SOURCE_KINDS:
                return {"name": name, "version": latest}
            if settings.get("kind") == "gitlab-tag":
                if source_available(source["source_url"]):
                    return {"name": name, "version": latest}
                return None
            if settings.get("kind") in ARGUMENT_SOURCE_KINDS:
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


def replace_argument(text, package, name, value):
    pattern = re.compile(rf"^(ARG\s+{re.escape(name)}=).*$", re.MULTILINE)
    updated, count = pattern.subn(
        lambda match: match.group(1) + str(value),
        text,
        count=1,
    )
    if count != 1:
        raise RuntimeError(f"{package}: expected one {name} argument")
    return updated


def update_package(path, name, version):
    packages = json.loads(CONFIG.read_text())
    settings = packages[name]
    manifest_path = path / "cpak.json"
    containerfile_path = path / "Containerfile"
    manifest_text = manifest_path.read_text()
    manifest = json.loads(manifest_text)
    current = manifest["version"]
    containerfile = containerfile_path.read_text() if containerfile_path.exists() else None
    source = upstream(settings, os.environ.get("GH_TOKEN"), manifest, containerfile)
    expected = source["cpak_version"]
    if version != expected:
        raise RuntimeError(f"release changed while updating {name}: {version} != {expected}")
    if current == version:
        return False

    if settings.get("kind") in ARGUMENT_SOURCE_KINDS:
        updated = containerfile
        if settings.get("replace_version", True):
            updated = updated.replace(current, version)
        for argument, field in settings.get("arguments", {}).items():
            if field not in source:
                raise RuntimeError(f"{name}: source has no {field} value")
            updated = replace_argument(updated, name, argument, source[field])
        manifest_text = replace_json_field(manifest_text, name, "version", current, version)
        json.loads(manifest_text)
        containerfile_path.write_text(updated)
        manifest_path.write_text(manifest_text)
        return True

    if settings.get("kind") == "gitlab-tag":
        containerfile = containerfile_path.read_text()
        matches = [
            match
            for match in ADD_RE.finditer(containerfile)
            if settings["source_match"] in match.group(4)
        ]
        if len(matches) != 1:
            raise RuntimeError(f"{name}: expected exactly one matching GitLab source")
        match = matches[0]
        digest = file_hash(source["source_url"])
        updated = (
            containerfile[: match.start(2)]
            + digest
            + containerfile[match.end(2) : match.start(4)]
            + source["source_url"]
            + containerfile[match.end(4) :]
        )
        manifest_text = replace_json_field(manifest_text, name, "version", current, version)
        json.loads(manifest_text)
        containerfile_path.write_text(updated)
        manifest_path.write_text(manifest_text)
        return True

    if settings.get("kind") in PACKAGE_SOURCE_KINDS:
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
        if settings.get("kind") in ("direct-deb-add", "html-asset"):
            matches = list(ADD_RE.finditer(containerfile))
            if len(matches) != 1:
                raise RuntimeError(f"{name}: expected exactly one checked source")
            match = matches[0]
            updated = (
                containerfile[: match.start(2)]
                + source["SHA256"]
                + containerfile[match.end(2) : match.start(4)]
                + source["url"]
                + containerfile[match.end(4) :]
            )
            manifest_text = replace_json_field(manifest_text, name, "version", current, version)
            json.loads(manifest_text)
            containerfile_path.write_text(updated)
            manifest_path.write_text(manifest_text)
            return True

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
