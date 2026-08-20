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

Official packages rebuild on a schedule with fresh platform layers. Packages
that download versioned GitHub release assets or use a Debian package repository
are also tracked by `upstreams.json`. The store checks them every day, updates
the version and source metadata together, then lets the package repository build
and verify the new image. Pre-release tracking must be enabled explicitly.

Stable Debian download endpoints can use the `direct-deb` provider. It first
compares the redirect target and content size, then downloads the package only
when either changed. The version is read from the package control data before
the checksum, size and URL are committed.

Add a package to `upstreams.json` only when its `Containerfile` uses a checked
GitHub release asset and the version in `cpak.json` matches that release. Sources
from a Debian repository must name its `Packages.gz` index, repository root and
package. In that case the published filename, size and SHA256 are read directly
from the signed repository metadata. Sources with another release API need their
own updater in the package repository.

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
