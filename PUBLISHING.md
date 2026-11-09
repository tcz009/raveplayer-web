# Publishing the web build

Current release: **1.6.80** (base `67a991f`). Offline suite: **1057 cases**
(1043 at 1.6.80, plus 14 warm-feed cases on `perf/web-warm`); browser smoke
suite: **43 cases**. `VERSION` is the single source of truth for
the image tag, and the image is built with `org.opencontainers.image.version`
from it plus the checked-out short sha (see `README.md`, “Release note”).

Web releases are git tags on `main`:

1. bump `VERSION` and update the counts in `README.md` (and this file);
2. run the suites in `README.md` → “Tests” and commit on `main`;
3. tag `vX.Y.Z` (`git tag vX.Y.Z && git push origin vX.Y.Z`);
4. deploy from that checkout with `HOST=user@server SSH_KEY=… ./scripts/deploy.sh`.

The image tag matches `VERSION`, so rollback is a redeploy of the previous
tag/checkout. Nothing in this repository pushes or deploys automatically.
