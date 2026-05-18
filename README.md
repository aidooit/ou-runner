# ou-runner

A Typer + Rich CLI that walks an Odoo database through OpenUpgrade major
versions (14 → 19) one hop at a time, using Docker to host the
PostgreSQL instance and each OpenUpgrade container.

`ou-runner` is a thin orchestrator: it clones the [OCA/OpenUpgrade][openupgrade]
branches you need, generates per-version Dockerfiles and a
`docker-compose.yml` to host them, and drives `pg_dump` / `pg_restore` /
`odoo -u all --stop-after-init` for you so each upgrade hop is a single
command. Everything lives in the directory you run it from — no global
state, nothing in your home folder.

> **Disclaimer.** Not affiliated with Odoo S.A. or the OCA OpenUpgrade
> project. `ou-runner` wraps [OCA/OpenUpgrade][openupgrade] for
> convenience; the migration scripts themselves belong to OCA.

[openupgrade]: https://github.com/OCA/OpenUpgrade

---

## Install

```bash
pip install ou-runner
```

Or, with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install ou-runner
```

---

## Prerequisites

- **Docker** (Desktop or Engine) running locally.
- **~10 GB free disk** — OpenUpgrade clones and intermediate dumps add up
  fast.
- **A PostgreSQL custom-format dump** of the Odoo 14 database you want to
  migrate (not a plain-text SQL file).
- **Python 3.10+** in the environment where you `pip install ou-runner`.

---

## Quick start

`ou-runner` writes all of its artifacts (dumps, backups, OpenUpgrade
clones, generated Dockerfiles, `docker-compose.yml`,
`migration_times.json`) into the **current working directory**. Pick a
folder per migration:

```bash
mkdir my-odoo-migration && cd my-odoo-migration

# 1. Bootstrap the sandbox for the version range you need.
ou-runner setup --from 14 --to 19

# 2. Drop your v14 dump where ou-runner expects it.
cp /path/to/your_v14_backup.dump dumps/odoo_14_db.dump

# 3. Migrate one hop at a time, testing between each.
ou-runner run --from 14 --to 15
# open http://localhost:8015, log in, smoke-test
ou-runner run --from 15 --to 16
# …

# Or, if you trust the chain end-to-end:
ou-runner run --from 14 --to 19 --hold-my-drink

# At any point:
ou-runner status      # progress chain, dumps, backups, migration times
```

When you're done, `ou-runner clean` sweeps every artifact `setup` and
`run` produced in the current directory. Docker containers and volumes
are not touched — run `docker compose down -v` if you want those gone
too.

---

## Commands

| Command | What it does |
| --- | --- |
| `setup --from <v> --to <v>` | Bootstrap the sandbox: clone OpenUpgrade branches, render Dockerfiles + `docker-compose.yml`. |
| `update` | `git pull` every `openupgrade_<v>/` clone to the latest commits on its branch. |
| `run --from <v> --to <v>` | Run one migration hop. Add `--hold-my-drink` to chain all hops up to `--to`. |
| `start <v>` | Boot a fresh, empty Odoo instance of one version (useful when you want to create a starter dump). |
| `status` | Show the migration's state: progress chain, recorded times, dumps on disk, backups grouped by version. |
| `logs <v> [-f]` | Tail the matching Odoo container's logs. |
| `clean [-y]` | Sweep every artifact `setup`/`run` produced in the current directory. |

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

Skipping versions is not supported — OpenUpgrade requires sequential
hops.

---

## License

MIT — see [LICENSE](./LICENSE).
