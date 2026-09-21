# Covalent sandbox images

Each Dockerfile in this folder builds one sandbox image satisfying the
Covalent sandbox contract v1 (`/bin/sh`, `tail -f /dev/null` keepalive,
declared runtime binaries on PATH, framework runners at `/runners/`).
Build context is always the repository root, because the runners are copied
from `src/covalent/skills/runners/`.

| Dockerfile | Image | Capabilities | Base | Notes |
|---|---|---|---|---|
| `Dockerfile.python` | `covalent-sandbox` | python, shell | python:3.12-alpine | Compatibility/default image. |
| `Dockerfile.nodejs` | `covalent-sandbox-node` | nodejs, shell | node:22-alpine | Native Node runtime. |
| `Dockerfile.polyglot` | `covalent-sandbox-polyglot` | python, nodejs, shell | python:3.12-alpine + node from node:22-alpine | One container for agents using both runtimes; node copied in, no apk. |
| `Dockerfile.slim` | `covalent-sandbox-slim` | python, shell | python:3.12-slim | glibc/manylinux wheels (numpy & co.) install without a compiler. |
| `Dockerfile.toolbox` | `covalent-sandbox-toolbox` | shell | alpine:3.22 | ~40 MB glue-work image with bash/git/curl/jq/unzip. |
| `Dockerfile.datascience` | `covalent-sandbox-datascience` | python, shell | python:3.12-slim | Preinstalled numpy/pandas/matplotlib/scipy; ~750 MB. |

Build any of them locally (tag `:dev`) and register the tag as a sandbox
profile image after validating it:

```bash
docker build -t covalent-sandbox-polyglot:dev -f sandbox/Dockerfile.polyglot .
docker build -t covalent-sandbox-slim:dev       -f sandbox/Dockerfile.slim .
docker build -t covalent-sandbox-toolbox:dev    -f sandbox/Dockerfile.toolbox .
docker build -t covalent-sandbox-datascience:dev -f sandbox/Dockerfile.datascience .
```

Production profiles should reference images by digest, not a floating tag.
