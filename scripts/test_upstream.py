import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import upstream


class UpstreamTest(unittest.TestCase):
    def test_every_official_package_has_an_upstream_policy(self):
        tracked = set(json.loads(upstream.CONFIG.read_text()))
        policies = json.loads((upstream.ROOT / "upstream-policies.json").read_text())
        covered = set()
        for mode, packages in policies.items():
            names = packages if isinstance(packages, list) else packages.keys()
            overlap = covered.intersection(names)
            self.assertFalse(overlap, f"duplicate {mode} policies: {sorted(overlap)}")
            covered.update(names)
        official = {
            path.parent.name
            for path in (upstream.ROOT / "categories").glob(
                "**/github/com/containerpak/*/manifest.json"
            )
        }
        self.assertEqual(tracked.intersection(covered), set())
        self.assertEqual(tracked.union(covered), official)

    def test_non_github_json_uses_a_generic_accept_header(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"version":"1.0"}'
        with mock.patch.object(upstream.urllib.request, "urlopen", return_value=response) as open_url:
            upstream.request_json("https://updates.example/releases.json", None)
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertIsNone(request.get_header("X-github-api-version"))

    def test_release_tag_normalizes_tag(self):
        release = {"tag_name": "release-4.2.0-stable"}
        settings = {
            "repository": "owner/package",
            "strip_prefix": "release-",
            "strip_suffix": "-stable",
        }
        with mock.patch.object(upstream, "request_json", return_value=release):
            self.assertEqual(upstream.release_tag(settings, "token"), "4.2.0")

    def test_json_release_reads_and_normalizes_version(self):
        settings = {
            "version_url": "https://versions.example/releases.json",
            "version_path": [0, "version"],
            "strip_prefix": "release-",
        }
        with mock.patch.object(
            upstream,
            "request_json",
            return_value=[{"version": "release-4.2.0"}],
        ):
            self.assertEqual(upstream.json_release(settings), {"cpak_version": "4.2.0"})

    def test_android_studio_reads_the_official_download_table(self):
        page = b'''<tr><td><button>android-studio-sample-linux.tar.gz</button></td>
        <td>1.6 GB</td><td>aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</td></tr>
        <a href="https://downloads.example/android/studio/ide-zips/2.0/android-studio-sample-linux.tar.gz">Download</a>'''
        body = io.BytesIO(page)
        body.__enter__ = lambda stream: stream
        body.__exit__ = lambda *args: None
        head = mock.MagicMock()
        head.__enter__.return_value = head
        head.headers = {"Content-Length": "42"}
        with mock.patch.object(
            upstream.urllib.request,
            "urlopen",
            side_effect=[body, head],
        ):
            source = upstream.android_studio_package(
                {"page_url": "https://downloads.example/studio"}
            )
        self.assertEqual(source["cpak_version"], "2.0")
        self.assertEqual(source["SHA256"], "a" * 64)
        self.assertEqual(source["Size"], "42")

    def test_pypi_package_uses_the_published_wheel_metadata(self):
        data = {
            "info": {"version": "4.2.0"},
            "urls": [
                {
                    "packagetype": "bdist_wheel",
                    "python_version": "py3",
                    "filename": "sample-4.2.0-py3-none-any.whl",
                    "digests": {"sha256": "f" * 64},
                    "size": 42,
                    "url": "https://files.example/sample-4.2.0-py3-none-any.whl",
                }
            ],
        }
        with mock.patch.object(upstream, "request_json", return_value=data):
            source = upstream.pypi_package({"package": "sample"})
        self.assertEqual(source["cpak_version"], "4.2.0")
        self.assertEqual(source["SHA256"], "f" * 64)

    def test_plan_requires_new_release_assets(self):
        packages = {
            "ready": {"repository": "owner/ready"},
            "missing": {"repository": "owner/missing"},
        }
        files = {
            ("Containerpak/ready", "cpak.json"): '{"version":"1.0"}',
            ("Containerpak/ready", "Containerfile"): self.containerfile("ready", "1.0"),
            ("Containerpak/missing", "cpak.json"): '{"version":"1.0"}',
            ("Containerpak/missing", "Containerfile"): self.containerfile("missing", "1.0"),
        }
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "upstreams.json"
            config.write_text(json.dumps(packages))
            with (
                mock.patch.object(upstream, "CONFIG", config),
                mock.patch.object(upstream, "repository_file", side_effect=lambda repo, path, token: files[(repo, path)]),
                mock.patch.object(upstream, "release_tag", return_value="2.0"),
                mock.patch.object(upstream, "source_available", side_effect=lambda url: "ready" in url),
            ):
                self.assertEqual(upstream.plan("token"), {"include": [{"name": "ready", "version": "2.0"}]})

    def test_plan_does_not_downgrade_a_package(self):
        packages = {"sample": {"repository": "owner/sample"}}
        files = {
            ("Containerpak/sample", "cpak.json"): '{"version":"2.0"}',
        }
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "upstreams.json"
            config.write_text(json.dumps(packages))
            with (
                mock.patch.object(upstream, "CONFIG", config),
                mock.patch.object(upstream, "repository_file", side_effect=lambda repo, path, token: files[(repo, path)]),
                mock.patch.object(upstream, "release_tag", return_value="1.9"),
            ):
                self.assertEqual(upstream.plan("token"), {"include": []})

    def test_update_rewrites_version_and_checksum(self):
        packages = {"sample": {"repository": "owner/sample"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = path / "upstreams.json"
            config.write_text(json.dumps(packages))
            (path / "cpak.json").write_text('{"version":"1.0"}\n')
            (path / "Containerfile").write_text(self.containerfile("sample", "1.0"))
            with (
                mock.patch.object(upstream, "CONFIG", config),
                mock.patch.object(upstream, "release_tag", return_value="2.0"),
                mock.patch.object(upstream, "file_hash", return_value="b" * 64),
            ):
                self.assertTrue(upstream.update_package(path, "sample", "2.0"))
            self.assertEqual(json.loads((path / "cpak.json").read_text())["version"], "2.0")
            containerfile = (path / "Containerfile").read_text()
            self.assertIn("sample-2.0.tar.gz", containerfile)
            self.assertIn("sha256:" + "b" * 64, containerfile)

    def test_update_rewrites_an_explicit_unchecked_source(self):
        packages = {"sample": {"repository": "owner/sample", "unchecked": True}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = path / "upstreams.json"
            config.write_text(json.dumps(packages))
            (path / "cpak.json").write_text('{"version":"1.0"}\n')
            (path / "Containerfile").write_text(
                "FROM scratch\nARG SAMPLE_VERSION=1.0\n"
                "RUN curl https://packages.example/${SAMPLE_VERSION}/sample.tar.gz\n"
            )
            with (
                mock.patch.object(upstream, "CONFIG", config),
                mock.patch.object(upstream, "release_tag", return_value="2.0"),
            ):
                self.assertTrue(upstream.update_package(path, "sample", "2.0"))
            self.assertIn("SAMPLE_VERSION=2.0", (path / "Containerfile").read_text())

    def test_update_rewrites_declared_arguments(self):
        packages = {
            "sample": {
                "kind": "go-release",
                "arguments": {
                    "SAMPLE_VERSION": "cpak_version",
                    "SAMPLE_SHA256": "SHA256",
                },
            }
        }
        source = {"cpak_version": "2.0", "SHA256": "d" * 64}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = path / "upstreams.json"
            config.write_text(json.dumps(packages))
            (path / "cpak.json").write_text('{"version":"1.0"}\n')
            (path / "Containerfile").write_text(
                f"FROM scratch\nARG SAMPLE_VERSION=1.0\nARG SAMPLE_SHA256={'a' * 64}\n"
            )
            with (
                mock.patch.object(upstream, "CONFIG", config),
                mock.patch.object(upstream, "upstream", return_value=source),
            ):
                self.assertTrue(upstream.update_package(path, "sample", "2.0"))
            containerfile = (path / "Containerfile").read_text()
            self.assertIn("ARG SAMPLE_VERSION=2.0", containerfile)
            self.assertIn("ARG SAMPLE_SHA256=" + "d" * 64, containerfile)

    def test_debian_repository_updates_runtime_source(self):
        packages = {
            "sample": {
                "kind": "debian-repository",
                "packages_url": "https://packages.example/Packages.gz",
                "base_url": "https://packages.example",
                "package": "sample",
            }
        }
        source = {
            "cpak_version": "2.0",
            "Filename": "pool/sample_2.0_amd64.deb",
            "SHA256": "c" * 64,
            "Size": "42",
            "url": "https://packages.example/pool/sample_2.0_amd64.deb",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = path / "upstreams.json"
            config.write_text(json.dumps(packages))
            manifest = {
                "version": "1.0",
                "runtime_sources": [
                    {
                        "name": "sample_1.0_amd64.deb",
                        "url": "https://packages.example/pool/sample_1.0_amd64.deb",
                        "sha256": "a" * 64,
                        "size": 21,
                        "installer": "dpkg",
                    }
                ],
            }
            (path / "cpak.json").write_text(json.dumps(manifest, separators=(",", ":")))
            (path / "Containerfile").write_text("FROM scratch\n")
            with (
                mock.patch.object(upstream, "CONFIG", config),
                mock.patch.object(upstream, "upstream", return_value=source),
            ):
                self.assertTrue(upstream.update_package(path, "sample", "2.0"))
            updated = json.loads((path / "cpak.json").read_text())
            self.assertEqual(updated["version"], "2.0")
            self.assertEqual(updated["runtime_sources"][0]["size"], 42)
            self.assertEqual(updated["runtime_sources"][0]["sha256"], "c" * 64)

    def test_release_updates_runtime_source(self):
        packages = {"sample": {"repository": "owner/sample"}}
        manifest = {
            "version": "1.0",
            "runtime_sources": [
                {
                    "name": "sample-1.0.deb",
                    "url": "https://packages.example/sample-1.0.deb",
                    "sha256": "a" * 64,
                    "size": 21,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = path / "upstreams.json"
            config.write_text(json.dumps(packages))
            (path / "cpak.json").write_text(json.dumps(manifest))
            (path / "Containerfile").write_text("FROM scratch\n")
            with (
                mock.patch.object(upstream, "CONFIG", config),
                mock.patch.object(upstream, "release_tag", return_value="2.0"),
                mock.patch.object(upstream, "file_metadata", return_value=("e" * 64, 42)),
            ):
                self.assertTrue(upstream.update_package(path, "sample", "2.0"))
            updated = json.loads((path / "cpak.json").read_text())
            self.assertEqual(updated["version"], "2.0")
            self.assertEqual(updated["runtime_sources"][0]["url"], "https://packages.example/sample-2.0.deb")
            self.assertEqual(updated["runtime_sources"][0]["size"], 42)

    def test_direct_deb_skips_an_unchanged_source(self):
        manifest = {
            "version": "1.0",
            "runtime_sources": [
                {
                    "name": "sample.deb",
                    "url": "https://packages.example/sample.deb",
                    "sha256": "a" * 64,
                    "size": 42,
                }
            ],
        }
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "https://packages.example/sample.deb"
        response.headers = {"Content-Length": "42"}
        with mock.patch.object(upstream.urllib.request, "urlopen", return_value=response):
            source = upstream.direct_debian_package(
                {"latest_url": "https://packages.example/sample.deb"},
                manifest,
            )
        self.assertEqual(source["cpak_version"], "1.0")
        self.assertEqual(source["SHA256"], "a" * 64)

    def test_gitlab_tag_uses_the_tag_commit(self):
        settings = {
            "tags_url": "https://gitlab.example/api/tags",
            "strip_prefix": "v",
            "source_url_template": "https://gitlab.example/archive/{commit}/source-{commit}.tar.gz",
        }
        tags = [{"name": "v2.0", "commit": {"id": "abc123"}}]
        with mock.patch.object(upstream, "request_json", return_value=tags):
            source = upstream.gitlab_tag(settings)
        self.assertEqual(source["cpak_version"], "2.0")
        self.assertEqual(
            source["source_url"],
            "https://gitlab.example/archive/abc123/source-abc123.tar.gz",
        )

    def test_github_release_asset_selects_a_prefixed_release(self):
        releases = [
            {"tag_name": "web-v2.0", "assets": []},
            {
                "tag_name": "desktop-v2.0",
                "assets": [
                    {
                        "name": "Sample-2.0.AppImage",
                        "browser_download_url": "https://packages.example/Sample-2.0.AppImage",
                        "digest": "sha256:" + "b" * 64,
                        "size": 42,
                    }
                ],
            },
        ]
        settings = {
            "repository": "owner/sample",
            "tag_prefix": "desktop-v",
            "asset_pattern": r"^Sample-{version}\.AppImage$",
        }
        with mock.patch.object(upstream, "request_json", return_value=releases):
            source = upstream.github_release_asset(settings, "token")
        self.assertEqual(source["cpak_version"], "2.0")
        self.assertEqual(source["SHA256"], "b" * 64)

    def test_go_release_reads_both_linux_architectures(self):
        releases = [
            {
                "version": "go2.0",
                "files": [
                    {"os": "linux", "arch": "amd64", "sha256": "a" * 64},
                    {"os": "linux", "arch": "arm64", "sha256": "b" * 64},
                ],
            }
        ]
        with mock.patch.object(upstream, "request_json", return_value=releases):
            source = upstream.go_release()
        self.assertEqual(source["cpak_version"], "2.0")
        self.assertEqual(source["sha256_arm64"], "b" * 64)

    def test_node_lts_reads_published_checksums(self):
        releases = [{"version": "v2.0", "lts": "Sample"}]
        checksums = (
            f"{'a' * 64}  node-v2.0-linux-x64.tar.xz\n"
            f"{'b' * 64}  node-v2.0-linux-arm64.tar.xz\n"
        )
        with (
            mock.patch.object(upstream, "request_json", return_value=releases),
            mock.patch.object(upstream, "request_text", return_value=checksums),
        ):
            source = upstream.node_lts()
        self.assertEqual(source["cpak_version"], "2.0")
        self.assertEqual(source["sha256_amd64"], "a" * 64)

    def test_zig_stable_ignores_the_development_build(self):
        releases = {
            "master": {"version": "3.0-dev"},
            "2.0": {
                "x86_64-linux": {
                    "tarball": "https://packages.example/zig-2.0.tar.xz",
                    "shasum": "c" * 64,
                }
            },
        }
        with mock.patch.object(upstream, "request_json", return_value=releases):
            source = upstream.zig_stable()
        self.assertEqual(source["cpak_version"], "2.0")
        self.assertEqual(source["SHA256"], "c" * 64)

    def test_html_asset_reads_the_version_and_checksum(self):
        page = (
            '<a href="https://packages.example/2.0/sample-2.0.AppImage">Download</a>'
        )
        head = mock.MagicMock()
        head.__enter__.return_value = head
        head.headers = {"Content-Length": "42"}
        settings = {
            "page_url": "https://packages.example/download",
            "link_pattern": (
                r'(?P<url>https://packages\.example/(?P<version>[0-9.]+)/'
                r'sample-[^" ]+\.AppImage)'
            ),
            "checksum_suffix": ".sha256sum",
        }
        with (
            mock.patch.object(
                upstream,
                "request_text",
                side_effect=[page, "e" * 64 + "  sample-2.0.AppImage\n"],
            ),
            mock.patch.object(upstream.urllib.request, "urlopen", return_value=head),
        ):
            source = upstream.html_asset(settings)
        self.assertEqual(source["cpak_version"], "2.0")
        self.assertEqual(source["SHA256"], "e" * 64)

    def test_debian_archive_skips_an_unchanged_release(self):
        source = {"cpak_version": "2.0"}
        with mock.patch.object(upstream, "debian_package", return_value=source):
            result = upstream.debian_archive({}, {"version": "2.0"})
        self.assertEqual(result, source)

    def test_direct_deb_add_skips_an_unchanged_source(self):
        url = "https://packages.example/sample-1.0.deb"
        containerfile = (
            "FROM scratch\n"
            f"ADD --checksum=sha256:{'a' * 64} {url} /tmp/sample.deb\n"
        )
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = url
        with mock.patch.object(upstream.urllib.request, "urlopen", return_value=response):
            source = upstream.direct_debian_add(
                {"latest_url": "https://packages.example/latest.deb"},
                {"version": "1.0"},
                containerfile,
            )
        self.assertEqual(source["cpak_version"], "1.0")
        self.assertEqual(source["SHA256"], "a" * 64)

    def test_direct_archive_reads_version_from_the_download(self):
        archive_data = io.BytesIO()
        payload = b'{"version":"2.0"}'
        with tarfile.open(fileobj=archive_data, mode="w:gz") as archive:
            member = tarfile.TarInfo("Sample/app/resources/app/package.json")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        content = archive_data.getvalue()

        head = mock.MagicMock()
        head.__enter__.return_value = head
        head.geturl.return_value = "https://packages.example/sample/2.0"
        head.headers = {"Content-Length": str(len(content))}
        download = io.BytesIO(content)
        download.__enter__ = lambda stream: stream
        download.__exit__ = lambda *args: None
        manifest = {
            "version": "1.0",
            "runtime_sources": [
                {
                    "name": "sample-1.0.tar.gz",
                    "url": "https://packages.example/sample/1.0",
                    "sha256": "a" * 64,
                    "size": 1,
                }
            ],
        }
        settings = {
            "latest_url": "https://packages.example/sample/latest",
            "version_member": "/app/resources/app/package.json",
            "name_template": "sample-{version}.tar.gz",
        }
        with mock.patch.object(
            upstream.urllib.request,
            "urlopen",
            side_effect=[head, download],
        ):
            source = upstream.direct_archive_package(settings, manifest)
        self.assertEqual(source["cpak_version"], "2.0")
        self.assertEqual(source["Filename"], "sample-2.0.tar.gz")
        self.assertEqual(source["Size"], str(len(content)))

    def test_debian_repository_updates_checked_source(self):
        packages = {
            "sample": {
                "kind": "debian-repository",
                "packages_url": "https://packages.example/Packages.gz",
                "base_url": "https://packages.example",
                "package": "sample",
            }
        }
        source = {
            "cpak_version": "2.0",
            "Filename": "pool/sample_2.0_amd64.deb",
            "SHA256": "d" * 64,
            "Size": "42",
            "url": "https://packages.example/pool/sample_2.0_amd64.deb",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = path / "upstreams.json"
            config.write_text(json.dumps(packages))
            (path / "cpak.json").write_text('{"version":"1.0"}\n')
            (path / "Containerfile").write_text(
                "FROM scratch\n"
                f"ADD --checksum=sha256:{'a' * 64} "
                "https://packages.example/pool/sample_1.0_amd64.deb /tmp/source\n"
            )
            with (
                mock.patch.object(upstream, "CONFIG", config),
                mock.patch.object(upstream, "upstream", return_value=source),
            ):
                self.assertTrue(upstream.update_package(path, "sample", "2.0"))
            containerfile = (path / "Containerfile").read_text()
            self.assertIn("sample_2.0_amd64.deb", containerfile)
            self.assertIn("sha256:" + "d" * 64, containerfile)

    def test_pypi_updates_hashed_wheel_url(self):
        packages = {"sample": {"kind": "pypi", "package": "sample"}}
        source = {
            "cpak_version": "2.0",
            "Filename": "sample-2.0-py3-none-any.whl",
            "SHA256": "f" * 64,
            "Size": "42",
            "url": "https://files.example/new-hash/sample-2.0-py3-none-any.whl",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = path / "upstreams.json"
            config.write_text(json.dumps(packages))
            (path / "cpak.json").write_text('{"version":"1.0"}\n')
            (path / "Containerfile").write_text(
                "FROM scratch\n"
                f"ADD --checksum=sha256:{'a' * 64} "
                "https://files.example/old-hash/sample-1.0-py3-none-any.whl /tmp/sample-1.0.whl\n"
            )
            with (
                mock.patch.object(upstream, "CONFIG", config),
                mock.patch.object(upstream, "upstream", return_value=source),
            ):
                self.assertTrue(upstream.update_package(path, "sample", "2.0"))
            containerfile = (path / "Containerfile").read_text()
            self.assertIn(source["url"], containerfile)
            self.assertIn("sha256:" + "f" * 64, containerfile)

    @staticmethod
    def containerfile(name, version):
        return (
            "FROM scratch\n"
            f"ADD --checksum=sha256:{'a' * 64} \\\n"
            f"    https://github.com/owner/{name}/releases/download/v{version}/{name}-{version}.tar.gz /opt/\n"
        )


if __name__ == "__main__":
    unittest.main()
