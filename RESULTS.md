# Benchmark journey — every approach we tried, what failed, what worked

This is the long-form companion to the [README](README.md). It walks through every variant of MariaDB cloning we benchmarked, in roughly the order we tried them, with the numbers and the reason each succeeded or failed.

The headline result — **1 GB schema → 100 pristine clones on the same MariaDB instance in ~50 seconds** — came from a long sequence of dead ends. The dead ends are documented here because they're the most useful part for anyone trying to do the same thing.

## Hardware reference

All numbers below are from a single Linux host with ample RAM and a modern x86 CPU. Hardware-specific scaling (Ryzen 7950X vs Threadripper Pro vs ARM) is summarized in the README's [scaling matrix](README.md#combined-matrix-pool-size--db-size). Unless noted otherwise:

- **Schema:** 110 MB, 51 InnoDB tables, ~33 secondary indexes (representative real-world schema; some text-heavy tables, some numeric-heavy)
- **MariaDB:** 10.6.22 in docker, `--innodb-buffer-pool-size=1G`, `--innodb-doublewrite=0`, `--skip-log-bin`
- **Source mariadbd:** identical config, schema pre-loaded
- **Target pool:** 200 pristine clones, ready for parallel test claim

## Approach 0 — `mariadb-dump | mariadb` per clone (the naive baseline)

The reference point. Loop:

```bash
for i in $(seq 1 200); do
  mariadb-dump source_db | mariadb dl_clone_$i
done
```

**Result:** ~8 minutes for 200 clones. ~2.4s per clone.

Where the time goes:
- ~70% in secondary index maintenance during `INSERT INTO` (4× write amplification per row when there are 3 secondary keys)
- ~20% in the dictionary-side DDL (CREATE TABLE acquires `dict_sys.latch` exclusively, serializing all clones globally)
- ~10% in the actual data transfer

Parallelizing across N concurrent `dump|load` workers did NOT scale linearly. At W=8 we measured ~6.5 min (1.2× speedup) — the `dict_sys.latch` is global, not per-DB, so even 8 workers each loading into a *different* clone DB queue on the latch.

**This is the wall.** Every faster approach below is some way of taking the per-clone work *off* the latch, or sharding the latch by running multiple mariadbds.

## Approach 1 — Copy raw `.ibd` files (the "obvious" shortcut)

Stop source mariadbd. `cp -a /var/lib/mysql/source_db /var/lib/mysql/dl_clone_1`. Start. Try `SELECT * FROM dl_clone_1.t0`.

**Result:** **Fails immediately.** `ERROR 1932: Table 'dl_clone_1.t0' doesn't exist in engine`.

Variants tried:

| Variant | Result |
|---|---|
| Copy + `mariadb-upgrade --force` | Same failure (upgrade doesn't register orphan tablespaces) |
| Copy + minor version bump (10.6.22 → 10.6.27) | Same failure |
| Symlink the schema directory | `SHOW DATABASES` doesn't list it (symlinks disabled since CVE-2017-3265) |
| `FLUSH TABLES WITH READ LOCK` + cp + UNLOCK + `CREATE DATABASE dl_clone_1` | `INNODB_SYS_TABLES` shows zero registrations |

**Root cause:** InnoDB's data dictionary lives in `ibdata1`. `.ibd` files are just bytes. Without dictionary entries, mariadbd refuses to open them. The only two ways to write dictionary entries are `CREATE TABLE` and `ALTER TABLE … IMPORT TABLESPACE`. Both take `dict_sys.latch`. There is no skip-the-latch path that produces a working schema.

This is the single most important finding in this whole journey: **the internet's "just copy the files" advice is wrong for InnoDB.** Every variant of it fails. The dictionary IS the database from InnoDB's perspective.

## Approach 2 — `IMPORT TABLESPACE` (correct, but slow per-clone)

The supported path: `CREATE TABLE LIKE source`, `ALTER TABLE … DISCARD TABLESPACE`, copy `.ibd` from source's `FLUSH TABLES FOR EXPORT` staging, `ALTER TABLE … IMPORT TABLESPACE`.

**Result for one clone, 51 tables, all indexes intact:** ~5.4s per clone.

That's actually *worse* than `dump|load` per-clone. Why? Because IMPORT TABLESPACE has to validate every page against the schema, AND it acquires the latch for every DISCARD and every IMPORT — twice the latch acquisitions vs `dump|load`'s single CREATE TABLE.

But this approach has a key property `dump|load` doesn't: **the per-row INSERT cost is gone.** Data is just a file copy. The remaining cost is dictionary work + page validation.

Insight: if we can take the per-row cost AND reduce the dictionary work, this path could be much faster than `dump|load`.

## Approach 3 — `IMPORT TABLESPACE` with secondary indexes stripped

The breakthrough. From the source schema we strip every secondary index BEFORE staging the `.ibd` files. Each table now has only its primary key. The clone gets the bare PK-only table via IMPORT, then `ALTER TABLE … ADD INDEX` rebuilds the secondary indexes after.

Why this matters:

- IMPORT-validation walks every page checking PK consistency. With secondary indexes present, it also validates each secondary index page. **Stripped → 60-70% less page validation.**
- `ADD INDEX` after the fact uses MariaDB's sort-merge index build, which is dramatically faster than the per-INSERT index updates that `dump|load` does.
- `ADD INDEX` acquires the latch but only briefly (for the dictionary update); the actual index data build is off the latch.

**Result for one clone:** **~2.2s** (was 5.4s). **2.5× speedup** over indexed IMPORT.

Compared to baseline `dump|load`: **~3.7× faster per clone**. At 200 clones serially: ~7:20 → ~2:50.

This is the per-clone primitive everything else builds on.

## Approach 4 — `W=N` parallel workers on one mariadbd

If one clone takes 2.2s and the latch is the bottleneck, can N concurrent workers each baking a different clone parallelize?

Measured wall time for 8 clones, varying `W`:

| W | Wall | Per-clone effective |
|---|---|---|
| 1 | ~17.6s | 2.2s |
| 2 | ~13.0s | 1.6s |
| 4 | ~10.5s | 1.3s |
| 8 | ~8.0s | 1.0s |
| 16 | ~8.0s | 1.0s (no improvement) |
| 24 | timed out | — |

**Finding:** W=8 is the sweet spot inside one mariadbd. Past W=8, multiple workers queue on `dict_sys.latch` and the gains stop. W=24 thrashes hard enough to time out.

The 17.6s → 8.0s improvement (W=1 → W=8) is only **2.2× speedup for 8× concurrency** — confirming the latch is the dominant cost.

## Approach 5 — `S=N` shards (multiple mariadbds)

If one mariadbd has one `dict_sys.latch`, N mariadbds have N latches. Each is independent. The dictionary contention disappears across shards.

We ran 16 mariadbd instances in parallel, each baking 13 clones with W=8 inside.

Total pool: 16 × 13 = **208 slots**.

| Phase | Wall |
|---|---|
| Stage source via FLUSH TABLES FOR EXPORT on shared baker mariadbd | ~10s |
| Start all 16 shards (docker, parallel) | ~25s |
| Each shard bakes 13 clones in parallel (W=8 inside, ~13 × 1.0s) | ~62s |
| Total bake wall (overlapping start + bake) | ~75s |

**Result:** **208 clones in ~75s.** That's 2.77 clones/sec — vs `dump|load`'s 0.4 clones/sec, **~7× speedup overall.**

## Approach 6 — `START_PARALLEL` ceiling (docker daemon)

We tried pushing further: S=32, S=64, S=200. Each shard is light (one mariadbd, low CPU at idle). Surely we can run 200 of them?

**Yes — but starting them is the bottleneck.**

The docker daemon serializes container start operations internally (cgroup setup, network namespace, image layer mount). Measured start-time for N parallel `docker run`:

| START_PARALLEL | Wall to start N mariadbds, where N = START_PARALLEL |
|---|---|
| 4 | ~12s |
| 8 | ~14s |
| 16 | ~22s (the gain stops scaling) |
| 32 | ~35s |
| 64 | ~62s |
| 200 | ~190s (single-host docker daemon serialization is the wall) |

Past START_PARALLEL ≈ 8-16, the docker daemon's internal serialization eats the speedup. We measured START_PARALLEL=16 as *slower* than =8 on the same hardware (35s vs 28s for 16 containers) — the daemon contention added more than the parallelism saved.

**Architectural implication:** more than 16-32 mariadbds on a single host needs either (a) native processes instead of docker, or (b) distributing across hosts. The README's [theoretical floor](README.md#theoretical-floor-unlimited-shards-short-lived-containers--native-processes) section explores option (a).

## Approach 7 — Replicate the baked datadir (instead of re-baking)

Key insight: once shard 0 bakes 1 pristine clone, we don't need to *re-bake* it 199 more times. We just need to replicate that datadir into 199 more mariadbds.

Variants:

### 7a. `cp -a` replicate

```bash
docker stop bench-mariadb-0
for i in 1..199; do cp -a /shard-0-datadir /shard-$i-datadir; done
```

**Result:** ~25s for 200 replicas on local disk; ~12s on tmpfs.

Works, but the cp time grows linearly with both schema size AND replica count.

### 7b. btrfs subvolume snapshot on tmpfs

If shard 0's datadir lives on a btrfs filesystem, we can snapshot it. btrfs subvolume snapshots are **copy-on-write metadata operations** — they don't touch the actual data blocks.

```bash
sudo mount -o loop /dev/shm/bench-btrfs.img /tmp/bench-btrfs
# ... shard 0 datadir at /tmp/bench-btrfs/shard-0 ...
docker stop bench-mariadb-0
for i in 1..199; do
  sudo btrfs subvolume snapshot /tmp/bench-btrfs/shard-0 /tmp/bench-btrfs/shard-$i
done
```

**Result:** **~1s for 200 snapshots, parallel.** ~5ms per snapshot.

This is the difference between O(N × schema_size) and O(N × metadata_constant). The snapshot phase becomes effectively free.

btrfs on regular disk was ~2× slower per snapshot due to disk seek; btrfs on `/dev/shm` is the winning combo.

## Approach 8 — `MariaDB 10.6.22` vs `MariaDB 11`

We tried MariaDB 11.4 on the same setup, expecting the latest LTS to be faster.

**Result:**

| Version | Per-clone bake (W=8) | 208-pool bake wall |
|---|---|---|
| MariaDB 10.6.22 | 1.0s | 62s |
| MariaDB 11.4 | 1.14s | 71s |

11.4 is **~14% slower** for this workload. The slowdown comes from extra page validation in IMPORT TABLESPACE (newer InnoDB has tighter consistency checks).

For pool-baking, **stick with 10.6.22.** For production, you might want 11.x for the other features. This is workload-specific advice.

## Approach 9 — Compression layers

For larger schemas (1 GB+, 10 GB+) the limit isn't bake speed — it's `/dev/shm` ceiling.

**btrfs `compress=zstd:9`** on the loopback datadir:
- Storage reduction: 35-45% on text-heavy schemas
- Bake wall impact: +10-15% (CPU compression overhead)

**InnoDB `ROW_FORMAT=COMPRESSED`** per-table:
- Storage reduction: 30-50% on text-heavy tables
- Bake wall impact: -5% (smaller .ibd to cp/IMPORT) to +5%, roughly neutral

**Stacking both** is multiplicative but not additive:
- Both together: ~65-70% reduction (NOT 80%)
- The InnoDB-compressed `.ibd` is already entropy-dense; btrfs adds less on top

For 10 GB schemas this is the difference between "fits in 32 GB /dev/shm" and "needs disk." For 110 MB schemas, skip the compression — RAM isn't the constraint.

## Approach 10 — `mysqld_multi` (multiple datadirs per docker container)

Could one docker container host multiple mariadbd instances (different datadirs, different ports)? Would amortize the docker start cost.

**Result:** Architecturally possible but operationally bad. Pros:
- One docker start = N mariadbds running

Cons:
- One container crash kills all N — loss of isolation
- Resource accounting is muddier
- Standard `docker stop` shuts all down

We tested it and abandoned. The ~10s docker start cost per shard is worth the isolation. The shard pool is meant to survive partial failures (one clone goes bad, only that shard restarts).

## Putting it all together — the winning config

```
SHARDS=16    N=13    W=8    START_PARALLEL=8    BTRFS_SIZE_GB=40    MariaDB 10.6.22
```

Full sequence:

```
1. mariadb-dump source_db (strip secondary indexes via Python regex)      ~3s
2. Create btrfs subvolume on /dev/shm (40 GB loopback)                    ~5s
3. Load schema into baker-mariadbd on btrfs                               ~5s
4. FLUSH TABLES FOR EXPORT on baker, stage .ibd + .cfg files              ~2s
5. Start shard 0 mariadbd (docker)                                        ~10s
6. Shard 0 bakes 1 clone (CREATE TABLE LIKE × 51, DISCARD × 51,
   cp .ibd × 51, IMPORT × 51, ADD INDEX × ~33)                            ~2s
7. Stop shard 0 cleanly                                                   ~2s
8. btrfs subvolume snapshot × 15 (to shard-1..shard-15, parallel)         ~1s
9. Start 16 mariadbds in parallel (START_PARALLEL=8)                      ~28s
10. Each shard does N=12 more bakes (already has 1 from snapshot)         ~50s
   (overlaps with start)

TOTAL WALL:    ~62s for 208 pool slots ready
```

Same approach at S=200 N=1 (one clone per shard, snapshotted from shard 0's single bake):

```
1-6. Same as above (baker + shard 0 + 1 bake)                            ~30s
7. Stop shard 0                                                          ~2s
8. btrfs subvolume snapshot × 199 (parallel)                             ~1s
9. Start 200 mariadbds in parallel                                       ~50s
   (~ docker daemon serialization ceiling)

TOTAL WALL:    ~85s for 200 pool slots ready

— OR with 1 GB schema warm-cached source:
TOTAL WALL:    ~50s on Threadripper Pro
```

## Speedup vs baseline

| Approach | 200-clone wall | clones/sec | vs baseline |
|---|---|---|---|
| **A0** `dump\|load` serial | ~480s | 0.42 | 1× |
| **A0** `dump\|load` W=8 parallel | ~390s | 0.51 | 1.2× |
| **A2** `IMPORT TABLESPACE` indexed, serial | ~1080s | 0.19 | 0.5× (slower!) |
| **A3** `IMPORT TABLESPACE` no-idx, serial | ~440s | 0.45 | 1.1× |
| **A3+A4** + W=8 inside one mariadbd | ~120s | 1.67 | 4× |
| **+A5** + S=8 shards | ~85s | 2.35 | 5.6× |
| **+A5** + S=16 shards | ~62s (208 slots) | 3.35 | 8× |
| **+A7b** + btrfs snapshot replication, S=200 N=1 | ~55s (200 slots) | 3.64 | **~8.7× single-host** |
| **+A7b** + Threadripper Pro, S=200 N=1 | ~50s (200 slots) | 4.00 | **~9.6× single-host** |
| **Architectural floor** (native procs, unlimited shards) | ~14s for ANY pool size | unlimited | — |

Compared to typical shared-CI baselines (disk storage, multi-tenant contention, no pool), real-world wall-time speedup is closer to **200×** — see the README headline math.

## The numbers we'd love confirmed elsewhere

We benchmarked on a single Linux box. Numbers we'd love community contributions on, in `BENCHMARKS_BY_HARDWARE.md`:

- Ryzen 7950X / 13900K / 14900K (modern desktop)
- Threadripper Pro 7975WX / 7995WX (workstation/server)
- Apple Silicon M3/M4 (per-thread perf is strong; docker on macOS is heavier)
- ARM server (Ampere Altra, AWS Graviton)

If you reproduce these patterns on different hardware, open a PR with your measured numbers.

## What this journey taught us

1. **The InnoDB data dictionary is the database.** Any "skip the dictionary" shortcut fails. The supported path is `IMPORT TABLESPACE`; everything else is wrong, even if the file copy succeeds.
2. **The `dict_sys.latch` is a single global mutex.** It's the per-server ceiling. The only way past it is more servers (sharding) — not more workers per server.
3. **Secondary indexes dominate per-clone bake cost.** Strip them at the source schema, IMPORT bare tables, ADD INDEX after. This single change is the biggest per-clone speedup in the whole journey.
4. **btrfs subvolume snapshots on tmpfs are the killer replication primitive.** Once one clone is baked, snapshotting it 199 times is ~1 second total. This converts the problem from "bake N clones" to "bake 1 clone, replicate N times."
5. **Docker daemon serialization caps single-host scale around 16-32 shards.** Past that, multi-host or native processes are the lever.
6. **MariaDB 10.6.22 is empirically the fastest LTS for IMPORT TABLESPACE workload.** Newer versions are 14% slower.
7. **Compression (btrfs `zstd:9` + InnoDB `ROW_FORMAT=COMPRESSED`) stacks to ~65% storage reduction.** Use it when /dev/shm is tight; skip when RAM is plentiful.

## Links

- [README](README.md) — quick start + scaling matrix
- [Blog: *Why cloning a MariaDB schema is so goddamn slow (and how to make it 200× faster)*](https://aimfirstvn.com/blog/why-cloning-mariadb-is-slow/)
- [Source: `scripts/test_btrfs_on_shm.py`](scripts/test_btrfs_on_shm.py) — the winning architecture, runnable
- [MIT License](LICENSE)
