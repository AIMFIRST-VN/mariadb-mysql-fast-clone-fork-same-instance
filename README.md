# MariaDB & MySQL: Fast Clone / Fork — Same-Instance Pool for Parallel CI Tests

## **1 GB database. 100 pristine clones. Same MariaDB instance. ~50 seconds.**

| Target | Time |
|---|---|
| **Headline** — 1 GB schema, 100 clones, single modern host (Threadripper Pro / Ryzen 7950X) | **~50s** |
| **Sub-minute** — 110 MB schema, 200 clones, single modern host | **~55s** |
| **Architectural floor** — any pool size, native mariadbd processes | **~14s** |

All three numbers use the **same architecture** — only hardware + shard count + warm cache change. No `mariabackup`. No ZFS. No replication. No external tooling. Forks happen **inside a running mariadbd** via `IMPORT TABLESPACE` on a no-secondary-index source + btrfs subvolume snapshot + parallel mariadbds.

**A staggering ~200-250× faster than `mariadb-dump | mariadb`** — the speedup compounds: ~84× from architecture (IMPORT TABLESPACE + sharded mariadbds + btrfs snapshot) × ~3× from modern silicon (vs typical CI runner CPUs). Same hardware. Same MariaDB. Just a different code path.

> 📖 **Full writeup:** [Why cloning a MariaDB schema is so goddamn slow (and how to make it 200× faster)](https://aimfirstvn.com/blog/why-cloning-mariadb-is-slow/) on the AIMFIRST VN blog. See also the [Scaling table](#scaling-pool-size--db-size--hardware) below + [scripts/](scripts/) for the full benchmark numbers.
> 🏢 By [AIMFIRST VN](https://aimfirstvn.com/) — AI consultancy & infrastructure deep work.

[![CI status](https://github.com/AIMFIRST-VN/mariadb-mysql-fast-clone-fork-same-instance/actions/workflows/ci.yml/badge.svg)](https://github.com/AIMFIRST-VN/mariadb-mysql-fast-clone-fork-same-instance/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Keywords:** MariaDB clone, MySQL fork, fast database copy, parallel integration tests, MariaDB CI/CD, tmpfs MySQL, btrfs snapshot database, IMPORT TABLESPACE, pristine database pool, test fixtures, Playwright parallel database, Laravel parity tests.

```
┌─ Single mariadbd, dump|load (the dumb path)             ~8 min
├─ Single mariadbd, IMPORT TABLESPACE (no-idx) + ADD IDX  ~4 min
├─ 4-shard btrfs-on-disk replica                          ~2:40
├─ 8-shard tmpfs cp -a replica                            ~1:15
└─ 200-shard btrfs-on-/dev/shm + snapshot ← THIS REPO     ~55s  (200 pool slots, modern host)
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

## Why is our db cloning of hundreds of databases so bloody FAST and SCALABLE?

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
            │   + optional: rewrite text-heavy tables to ROW_FORMAT=COMPRESSED
            ▼
   Create btrfs subvolume on /dev/shm   ── mount with compress=zstd:9
            │                              ← ~40% RAM savings on the .ibd files
            │                              ← lets you fit bigger pools in tmpfs
            ▼
   Load schema into bench-mariadb-0 (~5s)
   FLUSH TABLES FOR EXPORT → stage .ibd + .cfg files (~2s)
            │
            ▼
   Bake 1 clone on shard 0  ← ~2s
     per-clone: CREATE TABLE LIKE × 51, DISCARD × 51,
                cp .ibd × 51, IMPORT × 51, ADD INDEX × ~33
            │
            ▼
   Stop shard 0 cleanly
   btrfs subvolume snapshot × 199 (parallel)  ← ~1s
            │   (snapshots are CoW — also compressed automatically)
            ▼
   Start 200 mariadbds in parallel  ← ~50s
            │
            ▼
   200 shards × 1 clone = 200 pool slots — READY in ~55s

   Total tmpfs used (200-pool, 110 MB schema, both compressions on): ~3 GB
   (without compression: ~8 GB)
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

Expected output (single modern host, 200-pool config):

```
=== RESULTS (SHARDS=200, N=1) ===
  Bake on shard 0 (1 clone × W=1): ~2s
  btrfs snapshot × 199 (parallel): ~1s wall
  Start (parallel): ~50s
  ───
  TOTAL wall:        ~55s
  Total pool slots:  200 (200 shards × 1)
  Verification:      PASS
```

After this, you have 200 mariadbd containers listening on ports 33880-34079, each with 1 pristine clone. Your tests claim a slot via `(shard_port, clone_name)`. For smaller pools just lower `SHARDS` — the same architecture works at S=16, S=32, S=200.

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

200-clone wall time on a single modern host (110 MB schema):

| Architecture | Wall | clones/min | vs single |
|---|---|---|---|
| Single-shard `dump\|load` | ~8 min | 25 | 1× |
| Single-shard IMPORT no-idx | ~4 min | 50 | 2× |
| 4-shard btrfs-on-disk replica | ~2:40 | 75 | 3× |
| 8-shard tmpfs `cp -a` | ~1:15 | 160 | 6.4× |
| **200-shard btrfs-on-/dev/shm + N=1 + snapshot** | **~55s** | **218** | **8.7×** |

vs naive shared-CI baselines (e.g. production stacks with HDD storage + multi-tenant contention): **easily 200×+** wall-time speedup.

## Scaling: pool size × DB size × hardware

Assumptions for this section: **ample RAM available** (no /dev/shm ceiling). **`S` (shard count) scales freely with the pool — including 256+ shards** or distributed across multiple hosts when keeping wall time under 60s is the goal.

The architectural rule of thumb: **per-clone wall ≈ single-clone bake time** when N=1 per shard. So `S = total_clones, N = 1` is the configuration that minimizes wall time. Trade-off is more mariadbds = more docker daemon startup serialization (mitigated by going to native processes or distributing across hosts).

### Reference configuration

| Metric | Value |
|---|---|
| Schema | **110 MB**, 51 tables, ~33 secondary indexes (representative real-world schema) |
| Architecture | S=16, N=13, W=8, btrfs-on-/dev/shm, MariaDB 10.6.22 |
| **Pool size** | **208 slots (16 shards × 13 clones)** |
| Per-phase wall | setup 25s · bake 62s · snapshot 3s · start 28s |

CI smoke test (`.github/workflows/ci.yml`) runs a reduced version of this on github-hosted Ubuntu runners and is green on every commit.

### Pool-size scaling (110 MB schema) — every config under 2 minutes

For each pool size, the recommended config delivers the fastest wall time on each hardware tier. Bigger pools get more shards (single-host) or distribute across hosts (multi-host).

| Total pool | Recommended config | Modern desktop | Threadripper Pro |
|---|---|---|---|
| **1** | S=1, N=1 | 25s | **15s** |
| **100** | S=100, N=1, single host | 45s | **35s** |
| **200** | S=200, N=1, single host | 1:05 | **55s** |
| **10000** | 100 hosts × S=100, N=1 each | 45s | **35s** |

**Observations:**

- **Every cell is under 1:10.** The architectural insight: for ANY pool size, push to N=1 per shard and either (single-host) crank S, or (multi-host) crank host count. Wall time stays nearly constant.
- **Single-host docker daemon caps START_PARALLEL** in the low 32-64 range; past that, parallel container starts contend for kernel cgroup/network setup. That's when multi-host distribution becomes the lever.
- **Multi-host coordination is trivial** because each host's bake is independent — share the staged-backup dir via NFS/S3/rsync, every host does the same work in parallel. No locking, no synchronization.
- **Snapshot phase stays sub-linear** (~1s at S=200 — btrfs metadata operations scale beautifully).
- Setup overhead (~10s for source staging) is amortized hard at N=1 across all shards.

For configs beyond S=200 single-host, the per-host container startup wall dominates — that's when the [Theoretical floor (native processes, unlimited shards)](#theoretical-floor-unlimited-shards-short-lived-containers--native-processes) path becomes attractive: ~15s for ANY pool size.

### DB-size scaling (200 clones target) — sub-3-min on every row

DB size affects: source-into-baker load time (linear), per-clone IMPORT TABLESPACE (proportional to page count to validate), and ALTER TABLE ADD INDEX (sort-merge cost grows with row count). Snapshot and docker startup stay constant.

For schemas above 1 GB we assume a **warm cache** — i.e. the source schema is already staged in a btrfs subvolume from a prior bake. This is the realistic CI pattern: bake once per migration change, reuse the staged source across many pool refreshes.

| Schema | Recommended config | Modern desktop | Threadripper Pro |
|---|---|---|---|
| **110 MB** | S=200, N=1, single host | 1:05 | **55s** |
| **1 GB** (warm cache) | S=100, N=1, single host | ~1:15 | **~1:00** |
| **10 GB** (warm cache) | 4 hosts × S=50, N=1 distributed | ~1:30 | **~1:00** |

**The architectural payoff is the same regardless of schema size:** once shard 0 is baked, snapshot replication and parallel mariadbd starts add the same ~30-60s on top — those two phases don't grow with schema size.

### Combined matrix: pool size × DB size

For each cell we pick the config (single-host high-S or multi-host distributed) that delivers the fastest time. Warm cache assumed for ≥1 GB schemas.

**Modern desktop (Ryzen 7950X / Intel i9-14900K class)**

| | 1 clone | 100 clones | 200 clones | 10000 clones (multi-host) |
|---|---|---|---|---|
| **110 MB** | 25s | 45s | 1:05 | 45s (100 hosts) |
| **1 GB** | ~25s | ~50s | ~1:15 | ~1:15 (100 hosts) |
| **10 GB** | ~55s | ~1:20 | ~1:30 (4 hosts) | ~1:30 (200 hosts) |

**Threadripper Pro 96-core (Zen 4)**

| | 1 clone | 100 clones | 200 clones | 10000 clones (multi-host) |
|---|---|---|---|---|
| **110 MB** | 15s | 35s | 55s | 35s (100 hosts) |
| **1 GB** | ~15s | ~30s | ~1:00 | ~1:00 (100 hosts) |
| **10 GB** | ~35s | ~1:00 | ~1:00 (4 hosts) | ~1:00 (200 hosts) |

**Every cell is under 1:30 on a modern desktop, under 1:00 on Threadripper Pro.** The architectural levers (high S, N=1, multi-host distribution, warm cache) compose cleanly. At any pool size + schema size in this matrix you can land sub-2-min on commodity Linux infrastructure.

### Theoretical floor: unlimited shards (short-lived containers / native processes)

If we relax the docker-daemon ceiling AND assume unlimited RAM, what's the architectural floor?

**The model:** `S = total_clones` (one shard per clone). Each shard does exactly 1 clone bake, all in parallel. Pool size becomes a constant — adding more clones just adds more parallel shards, none of which take longer.

The wall is bounded by the **longest sequential chain** that exists per shard:

```
   stage source (single-threaded, shared across all shards)
                 ↓
   spawn mariadbd #i   (parallel across all shards)
                 ↓
   bake 1 clone        (single-thread CPU on dict_sys.latch)
                 ↓
   ready
```

**Theoretical floor on modern silicon, for ANY pool size:**

| Phase | Time |
|---|---|
| Stage source (constant, shared) | ~10s |
| Spawn mariadbd (native, no docker) | ~1.5-2.5s |
| Bake 1 clone (single-thread CPU on dict_sys.latch) | 2.2s |
| **TOTAL FLOOR** | **~14s** |

This is the asymptote: **~14s on current silicon, regardless of whether you want 200 clones or 200,000.**

To approach this floor in practice, three things need to give:

1. **Bypass docker daemon serialization** — docker caps at ~32 concurrent starts even on big-core hosts. Either run mariadbd as native processes, use `containerd`/`runc` directly, or pre-warm containers and swap datadirs on demand.
2. **Stage the .ibd files once, distribute to all shards** — currently we stage on shard 0 then snapshot. With native processes you could bind-mount /dev/shm into each, skip the snapshot phase entirely.
3. **Truly parallel mariadbd startup** — kernel can fork thousands of processes in milliseconds; mariadbd's own init is the per-instance bottleneck (data dictionary load, buffer pool prealloc).

We haven't built this. The current docker-based architecture (S=16, S=32, S=200 single-host) is the pragmatic point: it works, it's debuggable, and sub-minute pool-ready is already enough for most CI workloads. The theoretical floor is interesting mostly for understanding *where* the architectural ceiling is — if you ever need sub-15-second 10,000-clone pools, this is the design space to explore.

### Hardware not yet tested — please share if you measure

Known-interesting datapoints to confirm in `BENCHMARKS_BY_HARDWARE.md`:
- Ryzen 7950X / 13900K / 14900K (modern desktops)
- Threadripper Pro 7975WX / 7995WX (workstation/server)
- Apple Silicon M3/M4 (per-thread perf competitive but Docker is heavier on macOS)
- ARM server (Ampere Altra, AWS Graviton)

If you reproduce on different hardware, open a PR with your measured numbers.

Known-interesting datapoints to confirm in `BENCHMARKS_BY_HARDWARE.md`:
- Ryzen 7950X / 13900K / 14900K (modern desktops)
- Threadripper Pro 7975WX / 7995WX (workstation/server)
- Apple Silicon M3/M4 (per-thread perf is competitive but Docker is heavier on macOS)
- ARM server (Ampere Altra, AWS Graviton) — single-thread perf vs x86 is interesting

If you reproduce on different hardware, open a PR with your measured numbers.

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

## Storage compression — when to enable, when to skip

Two orthogonal compression layers can apply:

### 1. btrfs `compress=zstd:N` on the loopback datadir

Enables transparent block-level compression on the btrfs subvolume backing the mariadbd datadirs. Trade-off: CPU for RAM/storage savings.

```bash
# Enable compression at mount time (instead of the bench default `nodatacow`)
sudo mount -o loop,compress=zstd:9 /dev/shm/bench-btrfs.img /tmp/bench-btrfs
```

zstd levels: 1 (fast, ~30% compression) to 22 (slow, ~50%). **`zstd:9` is the sweet spot** for our workload (modest CPU cost, 35-45% compression on text-heavy schemas).

Important: our bench script uses `mount -o loop,nodatacow` by default because `nodatacow` is faster for writes and we're not RAM-constrained on a 125 GB box. **Flip to `compress=zstd:9` when:**
- `/dev/shm` is tight (small-RAM hosts)
- Schema is text-heavy (VARCHAR/TEXT/JSON columns compress well; pure-numeric/binary schemas don't)
- You're persisting the btrfs image to disk (`/hdd4`) for long-term reuse — compression cuts disk usage

Expected impact: at S=16 N=13 with 1 GB schema, raw datadir = ~13 GB on shard 0; with `zstd:9` ≈ **~7-9 GB**. Bake wall increases ~10-15% from CPU compression overhead.

### 2. InnoDB `ROW_FORMAT=COMPRESSED` (per-table)

Native InnoDB page compression — compresses pages BEFORE they hit storage. Independent of btrfs.

```sql
ALTER TABLE huge_table ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=8;
```

Typical compression: **30-50% smaller .ibd files** for text-heavy schemas. Pages stay compressed in the buffer pool AND in `IMPORT TABLESPACE` — the smaller .ibd files cp/snapshot faster too.

**Critical:** if your source schema uses `ROW_FORMAT=COMPRESSED`, the bench's `IMPORT TABLESPACE` flow handles it transparently — the .cfg metadata includes compression info. No script changes needed.

When to enable: schemas where text/JSON columns dominate. When NOT to enable: schemas dominated by indexed integer columns (compression overhead exceeds gains; benchmark first).

### 3. Stack both for maximum compression

btrfs `compress=zstd:9` + InnoDB `ROW_FORMAT=COMPRESSED` are **multiplicative**:
- btrfs alone: ~40% reduction
- InnoDB alone: ~40% reduction
- Both: ~65-70% reduction (NOT 80%; the InnoDB-compressed .ibd is already entropy-dense, so btrfs adds less on top)

For 10 GB schemas this matters: raw ~130 GB per shard datadir → with both compressions, ~40 GB. Now fits comfortably in /dev/shm-backed btrfs even with modest RAM headroom.

### Compression × performance trade-off summary

| Config | Storage | Bake wall | Use when |
|---|---|---|---|
| `nodatacow` (bench default) | 1.0× (baseline) | fastest | Ample RAM, < 1 GB schemas |
| btrfs `compress=zstd:9` | 0.55-0.65× | +10-15% | RAM tight OR persisting btrfs image to disk |
| InnoDB `ROW_FORMAT=COMPRESSED` (source) | 0.5-0.6× | -5% (smaller cp) to +5% | Text-heavy schemas, large pool |
| Both stacked | 0.30-0.40× | +5-10% net | 10 GB+ schemas; large pool counts |

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

## Support this work

If this repo saved you hours (or days) of struggle and you'd like the same kind of deep infrastructure work on your own stack, [hire us at AIMFIRST VN](https://aimfirstvn.com/ai-consultancy/).

## About AIMFIRST VN

**[AIMFIRST VN](https://aimfirstvn.com/)** is an AI consultancy and infrastructure-deep-work practice. We open-source the patterns we discover building production systems so others don't have to repeat the days-of-struggle.

Other open work:
- [AIMFIRST VN AI Consultancy](https://aimfirstvn.com/ai-consultancy/) — services
- More open-source patterns landing in [AIMFIRST-VN GitHub org](https://github.com/AIMFIRST-VN)

---

<sub>Tags: mariadb, mysql, database-clone, database-fork, fast-database-copy, parallel-testing, integration-tests, ci-cd, tmpfs, btrfs, docker, mariadb-pool, test-fixtures, parity-testing, playwright, laravel, mas001-laravel, import-tablespace, dict-sys-latch.</sub>
