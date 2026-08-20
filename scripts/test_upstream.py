import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import upstream


class UpstreamTest(unittest.TestCase):
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
