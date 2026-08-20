# Federated cpak Store System

cpak is decentralized by design: you can install any package by its Git origin:

```bash
cpak install github.com/containerpak/chrome
```

Optionally, you can register federated indexes ("stores") to search and
manage multiple collections of packages.

## Overview

A federated store is a Git repository containing a collection of categories,
each with a set of packages. Each package is defined by a `manifest.json` file
that describes the package's metadata.

## Submitting Apps to a Store

1. Fork or open a PR against the store repo.
2. Add your `manifest.json` under the correct category:
   ```
   Music/github/com/yourorg/yourapp/manifest.json
   ```
   as you can see, the path is a combination of the category and the Git origin
   of the package, following a Go-style package path convention.
3. On each PR commit, the CI bot automatically:
   * Validates the manifest schema
   * Attempts `cpak install <origin>` against the PR index.

Then moderators will review the PR and occasionally request changes. Then the
following steps will be taken:

1. A moderator run `!publish` in the PR to trigger the CI bot.
2. CI regenerates both `index.json` and `timestamp.json` in the same branch
   as the PR.
2. After a successful publish, maintainers merge the PR into the main branch,
   making the new package available to all users.

## Upstream Updates

Every official package has one update policy. `upstreams.json` tracks versioned
sources through GitHub and GitLab releases, Debian repositories, PyPI, published
JSON indexes, checked vendor downloads and release pages. The daily workflow
updates the package version, URL, checksum and size together, then lets the
package repository build and sign the new image. Pre-release tracking must be
enabled explicitly.

Packages installed from the cpak Ubuntu platform rebuild when its locked snapshot
changes. A package may instead own its updater, resolve its current toolchain at
build time or update itself at runtime. `upstream-policies.json` records these
cases and fails the tests if an official Store entry has no policy. Vendor sources
that require private credentials or expose no machine-readable index are listed
there as external gates.

Stable Debian endpoints use `direct-deb`; Debian repositories use their
`Packages.gz` metadata without downloading the package during a normal check.
Direct archives are downloaded only after their redirect target or size changes.
The updater also understands source arguments for multi-architecture SDKs, release
assets with vendor-specific names and checked archives published outside GitHub.

Add a package to `upstreams.json` when its source can be checked without guessing.
Use the package repository for an updater only when the source needs package-specific
logic. Add every other official package to exactly one group in
`upstream-policies.json`.

Run the same checks locally before changing an upstream definition:

```bash
python3 scripts/test_upstream.py
GH_TOKEN="$(gh auth token)" python3 scripts/upstream.py plan
```

## Managing Federated Stores

### `cpak store-add <uri>`

Registers a new remote store.

```bash
cpak store-add github.com/containerpak/store
```

### `cpak store-list`

Lists all configured remote stores:

```plain
cpak store-list

- github.com/containerpak/store        
- github.com/anotherorg/another-store/ 
```

### `cpak store-remove <uri>`

Removes a store by its URI:

```plain
cpak store-remove github.com/containerpak/store
```

## Searching & Installing Packages

```plain
cpak install spotify

1. github.com/spotifyltd/spotify
2. github.com/spotifier/spotify-ultra

Which cpak do you want to install? (1-2): 1
```

## Repository Structure

Each store repo should use this layout:

```
root/
├── categories/
├──── Music/
│     ├── github/com/org1/app1/manifest.json
│     │   ├── github/com/org2/app2/manifest.json
│     ├── index.json
│     └── ...
├── index.json
└── timestamp.json
```

* `manifest.json` files must match the manifest schema.
* `index.json` and `timestamp.json` are generated; do not edit.
