# 🚀 ou-runner

> **One command. Many Odoo majors. Zero hand-holding.**

If you've ever used [OCA/OpenUpgrade][openupgrade] in a Dockerized
setup, you already know the drill: switching branches for every
target major, rebuilding per-version images, hand-rolling `pg_dump`
and `pg_restore` between hops, and keeping a side note of how long
each one took.

`ou-runner` takes that loop off your hands. It walks an Odoo database
from Odoo version **X → Z** one hop at a time, in Docker, with every dump named,
every hop timed, and every artifact in the folder you ran it from.

> **Disclaimer.** Not affiliated with Odoo S.A. or the OCA OpenUpgrade
> project. `ou-runner` wraps [OCA/OpenUpgrade][openupgrade] for
> convenience; the migration scripts themselves belong to OCA.


> **Scope.** `ou-runner` was built and tested against an Odoo 14 → 19
> migration. Earlier majors (≤13) are out of scope — they may work,
> but nothing about the version table, Dockerfile rendering, or
> upgrade-path wiring has been exercised against them.

[openupgrade]: https://github.com/OCA/OpenUpgrade

---

## ✨ Features

- 🐳 **Docker-managed Odoo 14 → 19** — one compose file, one network, six profiles, integrated PGAdmin 
- 🔄 **Auto-clones OpenUpgrade branches** — no manual `git checkout`
- 💾 **Automated pg_dump between hops** — each hop's output is the next hop's input
- ⏱️ **Built-in migration timing** — `migration_times.json` records every run
- 🍺 **`--hold-my-drink`** — chain every hop from your starting version to the target
- 🧹 **`clean` sweeps everything** — no global state, nothing in your home folder

---

## Without vs. with ou-runner

```mermaid
flowchart LR
  subgraph WITHOUT["😩 Without ou-runner"]
    direction TB
    W1[Clone OpenUpgrade 15] --> W2[Write Dockerfile 15]
    W2 --> W3[docker run postgres + odoo 15]
    W3 --> W4[pg_restore v14 dump]
    W4 --> W5[odoo -u all --stop-after-init]
    W5 --> W6[pg_dump v15]
    W6 --> W7[Repeat for 16, 17, 18, 19...]
    W7 --> W8[Track times in a spreadsheet]
  end

  subgraph WITH["🚀 With ou-runner"]
    direction TB
    R1[ou-runner setup]
    R1 --> R2[ou-runner migrate --from 14 --to 19 --hold-my-drink]
    R2 --> R3[ou-runner status]
  end
```

---

## Install

With [uv](https://docs.astral.sh/uv/) (recommended):

```bash
uv tool install ou-runner
```

Or run it on-demand without installing — `uvx` spins up an ephemeral
environment, runs the command, and gets out of the way:

```bash
uvx ou-runner --help
uvx ou-runner setup
```

Or with pip:

```bash
pip install ou-runner
```

---

## Prerequisites

- **Docker** (Desktop or Engine) running locally.
- **~10 GB free disk** — OpenUpgrade clones and intermediate dumps add up
  fast.
- **A PostgreSQL custom-format dump** of the Odoo 14 database you want to
  migrate (not a plain-text SQL file).
- **Python 3.10+** available to `uv` (or `pip`) when installing.

---

## Quick start

`ou-runner` writes all of its artifacts (dumps, backups, OpenUpgrade clones, generated Dockerfiles, `docker-compose.yml`, `migration_times.json`) into the **current working directory**. 

Pick a folder per migration:

```bash
mkdir my-odoo-migration && cd my-odoo-migration
```

Bootstrap the sandbox. setup is interactive — it will ask you for the source and target Odoo versions.
```bash
ou-runner setup
```

Drop your v14 dump where ou-runner expects it.
```bash
cp /path/to/your_v14_backup.dump dumps/odoo_14_db.dump
```

Migrate one hop at a time, testing between each.
```bash
ou-runner migrate --from 14 --to 15
```
>open http://localhost:8015, log in, smoke-test

Repeat the process from other version
```bash
ou-runner migrate --from 15 --to 16
# …
```

Or, if you trust the chain end-to-end you can use the **--hold-my-drink** command; designed for braves and confident humans 🚀 
```bash
ou-runner migrate --from 14 --to 19 --hold-my-drink
```

At any point:
```shell
ou-runner status      # progress chain, dumps, backups, migration times
```

When you're done, `ou-runner clean` sweeps every artifact `setup` and `migrate` produced in the current directory. Docker containers and volumes are not touched — run `docker compose down -v` if you want those gone too.

> **Tip.** Every `ou-runner …` example below works just as well as `uvx ou-runner …` if you'd rather not install the tool globally.

---

## What happens during a hop

Every `ou-runner migrate --from X --to Y` runs these eight steps in order.
Failures halt the hop and (outside `--hold-my-drink`) drop you into an
interactive prompt so you can inspect, retry, or abort.

```mermaid
flowchart LR
  A[Ensure Postgres up] --> B[Stop running Odoo containers]
  B --> C[Restore source dump]
  C --> D[Backup before migration]
  D --> E[Start target version container]
  E --> F[odoo -u all --stop-after-init]
  F --> G[Restart container in normal mode]
  G --> H[pg_dump -Fc to dumps/odoo_to_db.dump]
```

---

## Chaining hops with `--hold-my-drink`

OpenUpgrade only supports sequential hops, so going from v14 to v19 means five upgrades. `--hold-my-drink` strings every intermediate hop together — each hop's dump becomes the next hop's input — and bypasses interactive prompts so a failure halts the chain cleanly.

```mermaid
flowchart LR
  D14[(dumps/odoo_14_db.dump)] --> H1[hop 14→15]
  H1 --> D15[(dumps/odoo_15_db.dump)] --> H2[hop 15→16]
  H2 --> D16[(dumps/odoo_16_db.dump)] --> H3[hop 16→17]
  H3 --> D17[(dumps/odoo_17_db.dump)] --> H4[hop 17→18]
  H4 --> D18[(dumps/odoo_18_db.dump)] --> H5[hop 18→19]
  H5 --> D19[(dumps/odoo_19_db.dump)]
```

---

## Commands

| Command | What it does |
| --- | --- |
| `setup` | Bootstrap the sandbox interactively (asks for source/target versions): clone OpenUpgrade branches, render Dockerfiles + `docker-compose.yml`. |
| `migrate --from <v> --to <v>` | Run one migration hop. Add `--hold-my-drink` to chain all hops up to `--to`. |
| `update` | `git pull` every `openupgrade_<v>/` clone to the latest commits on its branch. |1
| `start <v>` | Boot a fresh, empty Odoo instance of one version (useful when you want to create a starter dump). |
| `status` | Show the migration's state: progress chain, recorded times, dumps on disk, backups grouped by version. |
| `logs <v> [-f]` | Tail the matching Odoo container's logs. |
| `clean [-y]` | Sweep every artifact `setup`/`migrate` produced in the current directory. |

Run `ou-runner --help` (or `ou-runner <command> --help`) for full flag
listings.

---

## Versions

| Source | Target | Port | Output dump |
| --- | --- | --- | --- |
| 14 | 15 | 8015 | `dumps/odoo_15_db.dump` |
| 15 | 16 | 8016 | `dumps/odoo_16_db.dump` |
| 16 | 17 | 8017 | `dumps/odoo_17_db.dump` |
| 17 | 18 | 8018 | `dumps/odoo_18_db.dump` |
| 18 | 19 | 8019 | `dumps/odoo_19_db.dump` |

---

## How it works

`ou-runner` is a thin orchestrator — it never touches a database directly. Under the hood it:

- Clones the [OCA/OpenUpgrade][openupgrade] branches you need into `openupgrade_<version>/` folders in your CWD.
- Renders per-version `Dockerfile.openupgrade<N>` files and a single `docker-compose.yml` that ties them together.
- Drives `pg_dump`, `pg_restore`, and `odoo -u all --stop-after-init --upgrade-path=…` through `docker exec`, container by container.
- Records every hop's wall-clock time in `migration_times.json`.

>Everything lives in the directory you ran it from. No global state, nothing in your home folder.

### Sandbox layout (`14 → 19`)

Here is exactly what `ou-runner setup --from 14 --to 19` produces in your current directory — useful as a map when you first `ls` the sandbox:

```
my-odoo-migration/
├── dumps/
│   ├── odoo_14_db.dump          # input (you drop this)
│   └── odoo_15_db.dump … odoo_19_db.dump   # produced one per hop
├── backups/                     # pre-hop pg_dump snapshots
├── logs/
├── openupgrade_15/              # OCA/OpenUpgrade clone, branch 15.0
├── openupgrade_16/
├── openupgrade_17/
├── openupgrade_18/
├── openupgrade_19/
├── Dockerfile.openupgrade15     # FROM odoo:15.0 + openupgradelib
├── Dockerfile.openupgrade16
├── Dockerfile.openupgrade17
├── Dockerfile.openupgrade18
├── Dockerfile.openupgrade19
├── docker-compose.yml           # postgres + pgadmin + 5 openupgrade services
└── migration_times.json         # written after the first hop
```

| Item | What it holds |
| --- | --- |
| `dumps/odoo_14_db.dump` | Your starting database, dropped here before the first hop. |
| `dumps/odoo_<N>_db.dump` (N = 15…19) | Output of the `<N-1> → <N>` hop; input for the next hop. |
| `backups/` | Pre-migration `pg_dump -Fc` snapshots — one per hop, kept for rollback. |
| `logs/` | Captured Odoo container output. |
| `openupgrade_<N>/` | Shallow clone (`--depth 1`) of [OCA/OpenUpgrade][openupgrade] branch `<N>.0`. Mounted read-only into the matching container at `/mnt/openupgrade`. |
| `Dockerfile.openupgrade<N>` | Built on `odoo:<N>.0`; installs `openupgradelib` and `odoo_upgrade`. For v18+ pip runs with `--break-system-packages`; v19 also installs `html2text` and the Google API client libs. |
| `docker-compose.yml` | Three kinds of services: see compose table below. |
| `migration_times.json` | Wall-clock time of every completed hop — appears after the first successful `migrate`. |

The generated `docker-compose.yml` defines seven services:

| Service | Image | Host port | Notes |
| --- | --- | --- | --- |
| `postgres` | `postgres:17` | `5432` | Shared Postgres for every hop. Named volume `postgres_data`. Always up. |
| `pgadmin` | `dpage/pgadmin4` | `8088` | Web UI at `http://localhost:8088` (admin@admin.com / admin). |
| `openupgrade15` | built from `Dockerfile.openupgrade15` | `8015` | Profile `upgrade15`. Bind-mounts `./openupgrade_15` at `/mnt/openupgrade:ro`; data volume `odoo15_data`. |
| `openupgrade16` | built from `Dockerfile.openupgrade16` | `8016` | Profile `upgrade16`. Same shape as 15. |
| `openupgrade17` | built from `Dockerfile.openupgrade17` | `8017` | Profile `upgrade17`. |
| `openupgrade18` | built from `Dockerfile.openupgrade18` | `8018` | Profile `upgrade18`. |
| `openupgrade19` | built from `Dockerfile.openupgrade19` | `8019` | Profile `upgrade19`. |

Each `openupgrade<N>` service is gated by its compose profile, so only the version currently being migrated is brought up; `postgres` and `pgadmin` are always on.

---

## Contribute

`ou-runner` is free and open source — built in the open, MIT-licensed, no strings attached. If it saved you an afternoon of `pg_dump`/`pg_restore` juggling, the best thank-you is to make it better for the next person:

- 🐛 **Found a bug?** Open an [issue](https://github.com/aidooit/ou-runner/issues) — even a half-formed repro is useful.
- 💡 **Have an idea?** Feature requests, UX nits, doc gaps, naming bikesheds: all welcome.
- 🔧 **Want to send a patch?** Pull requests are very welcome. Small, focused PRs are the easiest to review and ship.
- 🗣️ **Just feedback?** Drop a note in [Discussions](https://github.com/aidooit/ou-runner/discussions) or the issue tracker — what worked, what didn't, which Odoo majors you actually walked through.

No CLA, no formal process — just kindness, a clear repro when you can manage one, and a willingness to iterate on review comments. Thanks for using it.

---

## License

MIT — see [LICENSE](./LICENSE).
