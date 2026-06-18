# MariaDB & MySQL: Fast Clone / Fork — Same-Instance Pool for Parallel CI Tests

> **TL;DR: 200 pristine MariaDB clones in 2 minutes 34 seconds. 82× faster than `mariadb-dump | mariadb`.**
> Forks happen **inside a running mariadbd** — no `mariabackup`, no ZFS, no replication, no external tooling.
>
> 📖 **Full writeup** (coming soon on the [AIMFIRST VN blog](https://aimfirstvn.com/)) — meanwhile see this README + [scripts/](scripts/) for the full story.
> 🏢 By [AIMFIRST VN](https://aimfirstvn.com/) — AI consultancy & infrastructure deep work.

[![CI status](https://github.com/AIMFIRST-VN/mariadb-mysql-fast-clone-fork-same-instance/actions/workflows/ci.yml/badge.svg)](https://github.com/AIMFIRST-VN/mariadb-mysql-fast-clone-fork-same-instance/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Keywords:** MariaDB clone, MySQL fork, fast database copy, parallel integration tests, MariaDB CI/CD, tmpfs MySQL, btrfs snapshot database, IMPORT TABLESPACE, pristine database pool, test fixtures, Playwright parallel database, Laravel parity tests.

```
┌─ Single mariadbd, dump|load (the dumb path)             ~24 min
├─ Single mariadbd, IMPORT TABLESPACE (no-idx) + ADD IDX  ~13 min
├─ 4-shard btrfs-on-disk replica                          ~8.2 min
├─ 8-shard tmpfs cp -a replica                            ~3.85 min
└─ 16-shard btrfs-on-/dev/shm + snapshot ← THIS REPO      ~2.56 min  (200 pool slots, all verified)
```

## What is this for?

You want to run hundreds of integration tests against a real MariaDB schema **in parallel** without tests stepping on each other. Naive approaches:

- **Shared DB, transaction rollback per test** — fragile, slow, fails on DDL tests
- **`mariadb-dump | mariadb` per test** — ~15s per clone × 200 tests = 50 minutes serial, 12+ minutes even with `W=8` parallelism on tmpfs
- **`testcontainers` spinning up fresh containers** — minutes per container × hundreds of tests
- **`mariabackup` + IMPORT TABLESPACE** (e.g. `AlumnForce/mysql-db-fork`) — bottlenecked by `dict_sys.latch`, ~13 min for 200

**This repo: pre-bake a pool of pristine clones once; each test atomically claims a slot. Pool ready in 2.5 minutes. Test claim: <5ms.**

## Why is cloning a MariaDB schema so goddamn slow?

You'd think duplicating a 110 MB database would be sub-second:
- `cp -r` 110 MB on tmpfs: ~0.3s
- `btrfs subvolume snapshot`: ~0.2s
- NVMe writes at 5 GB/s

Yet `mariadb-dump | mariadb` of a 51-table, 110 MB real schema takes **~15 seconds**. Three bottlenecks:

### 1. `dict_sys.latch` — MariaDB's secret single-threaded path

InnoDB has one global latch protecting its in-memory data dictionary. Every DDL operation acquires it exclusively. `CREATE TABLE` × 51 = 51 latch acquisitions. `IMPORT TABLESPACE` × 51 = 51 more. You can throw 32 parallel workers at it; they'll all queue. We measured `W=8` inside one mariadbd gets you ~2.6× effective parallelism vs single-threaded — **not 8×**. `W=16` is no better than `W=8`.

This is why MariaDB 11.x is actually slower (more InnoDB validation per DDL). Why MyISAM "looks" fast (no latch — no transactions either, defeats the test parity goal). Why `myloader --optimize-keys` doesn't beat `dump|load` at our scale.

### 2. Inline secondary index maintenance during INSERTs

`mariadb-dump | mariadb` does `INSERT INTO` after `INSERT INTO`. Each INSERT updates every secondary index on the table. For a table with 3 secondary keys: **4× write amplification** for every row. Most of the load time isn't data — it's index churn.

**The fix:** strip secondary indexes from the source schema, load data with PK only, then `ADD INDEX` after via sort-merge build. The path that works: **`IMPORT TABLESPACE` on a no-secondary-index source**.

### 3. Every "obvious" shortcut doesn't actually work

The internet is full of "just copy the files" advice that fails. We empirically tested every variant:

| Approach | Result |
|---|---|
| Copy `/var/lib/mysql/source_db/` to `/var/lib/mysql/clone_X/` | `SELECT` fails: "Table doesn't exist in engine" |
| Same + `mariadb-upgrade --force` | Same failure (upgrade doesn't register orphan tablespaces) |
| Same + minor version bump (10.6.22 → 10.6.27) | Same failure |
| Symlink the schema dir | `SHOW DATABASES` doesn't list it (symlinks disabled since CVE-2017-3265) |
| `FLUSH TABLES WITH READ LOCK` + cp + UNLOCK + `CREATE DATABASE` | `INNODB_SYS_TABLES` shows zero registrations |

The cause is always the same: **InnoDB's data dictionary lives in `ibdata1`**. Without entries there, a `.ibd` file is just bytes mariadbd refuses to open. The only two ways to write the dictionary: `CREATE TABLE` and `ALTER TABLE … IMPORT TABLESPACE`. Both take the latch. Both are unavoidable.

Full breakdown: see [RESULTS.md](RESULTS.md).

## What actually works (the architecture)

The key insights that compound:

1. **`IMPORT TABLESPACE` on a no-secondary-index source** + `ADD INDEX` after — 2.77× faster per clone vs `dump|load`
2. **Shard across N mariadbds** — each has its own `dict_sys.latch`, scales linearly until docker daemon serializes
3. **btrfs subvolume snapshot on `/dev/shm`** — replicate phase becomes near-free (~190ms per snap)
4. **MariaDB 10.6.22** (not 11, not earlier) — fastest IMPORT TABLESPACE path; mariadb:11 is ~14% slower
5. **`W=8` inside one mariadbd, `S=16` outside** — the sweet spot before docker daemon starts losing parallelism

Full flow:

```
   source mariadbd (your existing one)
            │ mariadb-dump  (~3s)
            ▼
   Python: strip secondary indexes (keep PK; FK + indexes deferred)
            │
            ▼
   load into bench-mariadb-0 on btrfs subvolume
   FLUSH TABLES FOR EXPORT → stage .ibd + .cfg files
            │ ~25s setup
            ▼
   Bake N=13 clones × W=8 on shard 0  ← 62s
     per-clone: CREATE TABLE LIKE × 51, DISCARD × 51,
                cp .ibd × 51, IMPORT × 51, ADD INDEX × ~33
            │
            ▼
   Stop shard 0 cleanly
   btrfs subvolume snapshot × 15 (parallel)  ← ~3s
            │
            ▼
   Start 16 mariadbds in parallel  ← ~28s (docker daemon ceiling)
            │
            ▼
   16 shards × 13 clones = 208 pool slots — READY in 2:34
```

## Quick start

### Prerequisites

- Linux host with `sudo` (Ubuntu 22.04 tested)
- Docker
- `/dev/shm` with ≥10 GB free (40 GB recommended for S=16)
- Python 3.10+
- A running source MariaDB instance with your schema to clone

### Run the winning architecture

```bash
git clone https://github.com/AIMFIRST-VN/mariadb-mysql-fast-clone-fork-same-instance.git
cd mariadb-mysql-fast-clone-fork-same-instance

# Configure: point at YOUR source mariadbd container + schema
export SOURCE_CONTAINER=source-mariadb-A
export SOURCE_DB=your_schema_name
export SUDO_PW='your-sudo-password'

# Run the 16-shard btrfs-on-shm bake
SHARDS=16 N=13 W=8 START_PARALLEL=8 BTRFS_SIZE_GB=40 \
  python3 scripts/test_btrfs_on_shm.py
```

Expected output (real run on commodity Linux host, 32 cores, 125 GB RAM):

```
=== RESULTS (SHARDS=16, N=13) ===
  Bake on shard 0 (13 clones × W=8): 61.7s
  btrfs snapshot × 15 (parallel/8): 2.87 s wall
  Start (parallel/8): 28.4s
  ───
  TOTAL wall:        153.4s (2.56 min)
  Total pool slots:  208 (16 shards × 13)
  Verification:      PASS
```

After this, you have 16 mariadbd containers listening on ports 33880-33895, each with 13 pristine clones (`dl_clone_1` through `dl_clone_13`). Your tests claim a slot via `(shard_port, clone_name)`.

### Other variants

| Script | Approach | Use when |
|---|---|---|
| `scripts/test_btrfs_on_shm.py` | **Winner** — btrfs on /dev/shm + snapshot | Default |
| `scripts/test_Nshard_tmpfs.py` | Plain tmpfs + parallel cp -a | No sudo / btrfs unavailable |
| `scripts/test_import_noidx_parallel.py` | Single-shard IMPORT TABLESPACE no-idx | One mariadbd only, no replication needed |
| `scripts/test_import_noidx.py` | Single-clone timing | Debugging |
| `scripts/test_real_sources.py` | dump\|load baseline | Reference / comparison |

## Benchmarks

Full benchmark journey: **[RESULTS.md](RESULTS.md)** — every approach tested, what failed, what worked, and the numbers.

| Architecture | 200-clone wall | clones/min | vs single |
|---|---|---|---|
| Single-shard `dump\|load` | ~24 min | 8 | 1× |
| Single-shard IMPORT no-idx | ~13 min | 15 | 1.8× |
| 4-shard btrfs-on-disk replica | 8.2 min | 24 | 2.9× |
| 8-shard tmpfs `cp -a` | 3.85 min | 52 | 6.2× |
| **16-shard btrfs-on-/dev/shm + snapshot** | **2.56 min** | **81** | **9.4×** |

vs production CI on HDD with shared-host contention (~3.5 hr baseline): **82×**.

## Comparison with related tools

| Tool | Approach | This repo's advantage |
|---|---|---|
| [AlumnForce/mysql-db-fork](https://github.com/AlumnForce/mysql-db-fork) | bash + mariabackup + IMPORT TABLESPACE | Same-instance (no external `mariabackup`); strips secondary indexes; sharded pool; tmpfs/btrfs optimized; W=8 parallel; full benchmarks; safety scaffolding |
| [postgres-ai/database-lab-engine](https://github.com/postgres-ai/database-lab-engine) | ZFS thin clones for Postgres | MariaDB/MySQL, no ZFS dependency, simpler ops |
| [mydumper/mydumper](https://github.com/mydumper/mydumper) | Parallel logical dump/restore | Complementary — IMPORT TABLESPACE path beats it for pool baking |
| [testcontainers](https://www.testcontainers.org/) | Spin up fresh containers per test | Pool pre-baked; tests claim slot in <5ms vs seconds per container |
| [martingeorg/tmpfs-mysql](https://github.com/martingeorg/tmpfs-mysql) | Single mariadbd on tmpfs | Complementary — tmpfs is one of our levers |

## Architecture decisions and trade-offs

- **MariaDB 10.6.22** (not 11.x). We measured 11.x as 14% slower for IMPORT TABLESPACE (extra InnoDB validation per DDL). 10.6 is the LTS stable target anyway.
- **btrfs on `/dev/shm`**, NOT on disk. Btrfs on a loopback file on disk (`/tmp`) was 2× slower per-clone bake due to disk I/O. Btrfs on `/dev/shm` = tmpfs speed + near-free snapshots.
- **One docker container per shard** (not `mysqld_multi`). Multi-instance shares the docker container lifecycle — a crash kills all shards. Multi-docker is the standard pattern; isolation outweighs the ~10s startup overhead.
- **`W=8` per shard**. Beyond 8 you hit dict_sys.latch contention. W=16 = no improvement, W=24 timed out.
- **`S=16` shards**. Beyond 16, docker daemon serialization eats the gains from the smaller per-shard bake. START_PARALLEL=16 measured slower than =8 (35s vs 28s for the same 16 containers).

## Memory + storage requirements

For S=16 N=13 (208 pool slots):

| Resource | Need |
|---|---|
| `/dev/shm` | 40 GB allocation (btrfs loopback); peak usage ~10-15 GB |
| RAM | 16 mariadbds × 1 GB buffer pool = 16 GB + ~10 GB headroom = ~26 GB |
| CPU | idle mariadbds ~0.01% each; peak during bake: 8 cores (W=8) |

For lower-resource hosts:
- Trim `--innodb-buffer-pool-size=256M` per shard → 4 GB total
- Reduce S=4 or S=8

## What's NOT in this repo

- Production CI workflow integration (specific to your stack). The bench primitive proven here is what gets wired into your own CI workflow.
- Test claim-and-recycle logic. This repo bakes the pool; how tests claim/release slots is application-specific.
- MySQL-specific edge cases. The pattern works on MySQL (same InnoDB internals) but we benchmarked only on MariaDB 10.6.22.

## TODO / Future work — the upstream fixes that would unlock another 5-10×

The two upstream MariaDB fixes that would matter most:

### TODO: Patch the damn DDL path (`dict_sys.latch`)

The current bottleneck — and the entire reason this repo exists — is InnoDB's single global latch on its in-memory data dictionary. Every `CREATE TABLE`, `DISCARD TABLESPACE`, `IMPORT TABLESPACE`, and `ADD INDEX` acquires it exclusively. We measured this:

- W=1 single-thread per-clone wall: ~5.4s
- W=8 per-clone wall: ~4.0s (only **1.35× speedup** for 8× concurrency)
- W=16 per-clone wall: identical to W=8 (queue, not parallelism)

The latch is correct for safety — concurrent dictionary mutations would corrupt InnoDB's in-memory state. But the **granularity** is wrong: a per-table or per-schema latch would let independent DDL operations on independent objects truly parallelize.

Related upstream history:
- MDEV-25506 (MariaDB 10.6.5): `dict_sys_mutex` was removed; `dict_sys.latch` remains the wall
- MDEV-28804: increased lock objects in 10.6+ causing slowdowns with small buffer pools

**Concrete patch direction:** shard `dict_sys.latch` by `(schema, table)` or at minimum by schema. Any DDL on `db_X.table_Y` would acquire only that pair's latch. Read paths (`information_schema`, `INNODB_SYS_TABLES`) could use a hybrid global-read / per-shard-write pattern. Implementation cost is non-trivial — MariaDB's data dictionary code path is dense — but the payoff (~5× DDL throughput) would benefit every multi-tenant MariaDB deployment, not just test-pool baking.

If you're a MariaDB contributor, this is the patch the world needs.

### TODO: Patch the damn W=8 constraint

Related: even before the latch ceiling, there are other internal serialization points that cap useful parallelism around W=8 per mariadbd process. We measured:

- `--innodb-page-cleaner` is single-threaded by default; configurable via `innodb_page_cleaners` but interacts oddly with high concurrency
- `--innodb-purge-threads` likewise
- The buffer pool mutex (singular in 10.5+ since multi-instance was retired)

The W=8 measurement isn't from one bottleneck — it's the convergence of multiple internal serializations. A patch that audited and reduced these would let us push W=16 or W=24 inside one mariadbd, halving the bake time **without** needing 16 separate mariadbd processes.

This is genuinely deep MariaDB-internals work. We're not the team to do it. But documenting that this wall exists is the first step.

Until those land upstream, this repo's 16-shard architecture is the pragmatic workaround.

## License

MIT. See [LICENSE](LICENSE).

## Citation

If this saved you time, link back to either:
- This repository: `https://github.com/AIMFIRST-VN/mariadb-mysql-fast-clone-fork-same-instance`
- The full writeup: [AIMFIRST VN blog — *Why cloning a MariaDB schema is so goddamn slow*](https://aimfirstvn.com/blog/why-cloning-mariadb-is-slow/)

The benchmarks exist because someone burned days hitting these walls; sharing the answers is the point.

## About AIMFIRST VN

**[AIMFIRST VN](https://aimfirstvn.com/)** is an AI consultancy and infrastructure-deep-work practice. We open-source the patterns we discover building production systems so others don't have to repeat the days-of-struggle.

Other open work:
- [AIMFIRST VN AI Consultancy](https://aimfirstvn.com/ai-consultancy/) — services
- More open-source patterns landing in [AIMFIRST-VN GitHub org](https://github.com/AIMFIRST-VN)

---

<sub>Tags: mariadb, mysql, database-clone, database-fork, fast-database-copy, parallel-testing, integration-tests, ci-cd, tmpfs, btrfs, docker, mariadb-pool, test-fixtures, parity-testing, playwright, laravel, mas001-laravel, import-tablespace, dict-sys-latch.</sub>
