# lineage-ota-server

Self-hosted OTA metadata + download server implementing the JSON contract
expected by `packages/apps/Updater`'s `UpdatesNetworkDataSource`
(`GET {url}/api/v2/devices/{device}/builds`, HTTPS-only, no redirects).

## How it works

- Build zips live at `BUILDS_DIR/<device>/*.zip`, named
  `lineage-<version>-<YYYYMMDD>-<type>-<device>[-signed].zip`
  (e.g. `lineage-23.0-20260701-nightly-vim3-signed.zip`).
- `GET /api/v2/devices/{device}/builds` scans that directory (cached
  `SCAN_CACHE_TTL_SECONDS`, default 60s) and returns build metadata, newest
  first. sha256 is computed once per file and cached on disk
  (`BUILDS_DIR/.sha256_cache.json`, keyed by path+size+mtime).
- `GET /download/{device}/{filename}` streams the zip.
- `POST /api/v2/devices/{device}/builds` (multipart, `file` field) uploads a
  new build. Requires `Authorization: Bearer $UPLOAD_TOKEN`. After a
  successful upload, only the newest `RETAIN_BUILDS` (default 5) files for
  that device are kept — older zips and their sidecars are deleted.
- Per-build metadata (`os_patch_level`, `os_sdk_level`, `datetime`) can be
  overridden by dropping/uploading a `<filename>.json` sidecar, or passing
  them as form fields on upload; otherwise `datetime` falls back to the
  zip's mtime.

## Config (env vars)

| Var | Default | Purpose |
|---|---|---|
| `BUILDS_DIR` | `/builds` | Root of the per-device build directories |
| `BASE_URL` | `https://updates.schuurman-it.com` | Used to build the `url` field of downloads |
| `SCAN_CACHE_TTL_SECONDS` | `60` | How long a device's directory listing is cached |
| `RETAIN_BUILDS` | `5` | Builds kept per device; older ones deleted on upload |
| `UPLOAD_TOKEN` | unset | Bearer token required for uploads; uploads return 503 if unset |

## Uploading a build (from your build server)

```sh
curl -sf -X POST "https://updates.schuurman-it.com/api/v2/devices/vim3/builds" \
  -H "Authorization: Bearer $UPLOAD_TOKEN" \
  -F "file=@lineage-23.0-20260701-nightly-vim3-signed.zip" \
  -F "os_patch_level=2026-07-01" \
  -F "os_sdk_level=36"
```

## Local dev

```sh
pip install -r app/requirements.txt
BUILDS_DIR=./builds UPLOAD_TOKEN=dev uvicorn app.main:app --reload --port 8080
```

## Release (CI)

`.github/workflows/release.yml` builds and pushes on every `vX.Y.Z` tag:

- Docker image: `ghcr.io/gschuurman/lineage-ota-server:X.Y.Z` (+ `:latest`)
- Helm chart (OCI): `oci://ghcr.io/gschuurman/charts/lineage-ota-server`, version `X.Y.Z`

```sh
git tag v0.1.0
git push origin v0.1.0
```

GHCR packages are created private by default. After the first run, open
the package on github.com (Package settings) and either link it to this
repo (grants access via the repo's visibility) or make it public — otherwise
the cluster needs an `imagePullSecret` to pull it.

## Deploy with Helm

```sh
helm registry login ghcr.io -u gschuurman
helm upgrade --install lineage-ota oci://ghcr.io/gschuurman/charts/lineage-ota-server \
  --version 0.1.0 \
  --set baseUrl=https://updates.schuurman-it.com \
  --set ingress.host=updates.schuurman-it.com \
  --namespace lineage-ota --create-namespace
```

Or run straight from the checked-out chart during development:

```sh
helm upgrade --install lineage-ota helm/lineage-ota-server \
  --set image.tag=0.1.0 \
  --set baseUrl=https://updates.schuurman-it.com \
  --set ingress.host=updates.schuurman-it.com \
  --namespace lineage-ota --create-namespace
```

The chart auto-generates a random `UPLOAD_TOKEN` on first install and keeps
it stable across upgrades (stored in a Secret). Read it back to configure
your build server:

```sh
kubectl -n lineage-ota get secret lineage-ota-lineage-ota-server \
  -o jsonpath='{.data.upload-token}' | base64 -d; echo
```

Or set your own token explicitly with `--set uploadToken=<token>`, or point
at a Secret you manage yourself with `--set existingSecret=<name>
--set existingSecretKey=<key>`.

By default the chart provisions a 200Gi PVC (`persistence.size`) for
`/builds`; point it at an existing volume with
`--set persistence.existingClaim=<pvc-name>`.

## Device-side config

`device/khadas/vim3/overlay/packages/apps/Updater/app/src/main/res/values/strings.xml`
already overrides `updater_server_url` to
`https://updates.schuurman-it.com/api/v2/devices/{device}/builds` — no
further device-tree changes are needed once this server is live.
