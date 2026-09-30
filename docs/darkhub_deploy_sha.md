# DarkHub deployment SHA (USR-67)

The previous compose file named both a `build` context at `DarkFactory.git#main`
and a reusable image tag (`darkhub:v2.1`). Dokploy stores the compose definition
separately from this repository. A completed job titled `Manual deployment` is
consistent with Dokploy's `raw` source mode: that mode writes its stored
`composeFile` and runs Docker Compose. It does not clone this repository to
refresh that file. Whether the stored file currently contains `build:` or only
`image:` could not be confirmed here: the read-only `project.all` request was
blocked locally by WinError 10013. The short job and null SHA therefore do not
prove whether the old container came from a cached Git build or a ready image.

The previous `compose.saveEnvironment` call updated Dokploy's `.env`, but could
not repair a stored compose file that omitted the SHA mapping. Even with the
repository's compose file, `#main` was mutable, so the build source and the
reported SHA were independent. `git_sha: null` on `/health` proves that the
running process did not receive a nonempty `DARKFAC_GIT_SHA`.

The redeploy script now reads the compose file from the exact `origin/main`
commit, renders that full SHA into the Git build context, build arg and runtime
environment, and installs it as Dokploy's raw compose file before deploy. It
does not replace the existing Dokploy environment block. The compose forces a
build, disables build cache and uses the SHA as the Git ref. The script then
waits for `/health` to report that same SHA and fails if it does not converge.

Dokploy's [Compose API](https://docs.dokploy.com/docs/api/compose) documents
`sourceType`, `composeFile`, and `compose.update`. Its
[deploy implementation](https://github.com/Dokploy/dokploy/blob/canary/packages/server/src/services/compose.ts)
shows the raw versus Git source paths and the default manual title. Docker
documents [full SHA Git contexts](https://docs.docker.com/build/concepts/context/)
and [Compose build cache controls](https://docs.docker.com/reference/compose-file/build/).
