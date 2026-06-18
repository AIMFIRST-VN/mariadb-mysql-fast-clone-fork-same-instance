#!/usr/bin/env python3
"""S=16 with btrfs-loopback ON /dev/shm (RAM-backed btrfs).

Previous btrfs test was slow because the loopback file was on /tmp (disk).
Move the loopback to /dev/shm (tmpfs/RAM) and we should keep tmpfs bake speed
AND get sub-second snapshots that replace 10-16s cp -a per replica.

Flow:
  1. Create 40 GB btrfs loopback file on /dev/shm → mkfs.btrfs → mount.
  2. Create subvolume shard-0; spin bench-mariadb-0 with subvol as datadir.
  3. Stage source + bake N_PER_SHARD clones (IMPORT TABLESPACE no-idx, W=8).
  4. Stop shard 0 cleanly.
  5. btrfs subvolume snapshot shard-0 → shards 1..S-1 (parallel).
  6. Start all S mariadbds in parallel.
  7. Verify each shard has the pool.
"""
import os, re, sys, subprocess, time
import concurrent.futures as cf

ROOT_PW = "root"
N_PER_SHARD = int(os.getenv("N", "13"))
W = int(os.getenv("W", "8"))
SHARDS = int(os.getenv("SHARDS", "16"))
COPY_PARALLEL = int(os.getenv("COPY_PARALLEL", "8"))
START_PARALLEL = int(os.getenv("START_PARALLEL", "8"))
MARIA_IMAGE = os.getenv("MARIA_IMAGE", "mariadb:10.6.22")
SOURCE_CONTAINER = os.getenv("SOURCE_CONTAINER", "source-mariadb-A")
SOURCE_DB = os.getenv("SOURCE_DB", "your_schema")

BTRFS_IMG = "/dev/shm/bench-btrfs.img"
BTRFS_MNT = "/tmp/bench-btrfs"
BTRFS_SIZE_GB = int(os.getenv("BTRFS_SIZE_GB", "40"))

SRC_DB = "source_db_noidx"
BACKUP_DIR = "/dev/shm/bench-backup-noidx"
SUDO_PW = os.environ.get("SUDO_PW", "")

TABLES = []
INDEXES_PER_TABLE = {}


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def sudo(cmd_str, timeout=300):
    """Run cmd_str as root. If SUDO_PW is set, pipe it via stdin; otherwise
    rely on NOPASSWD sudoers config (e.g. on github-hosted runners)."""
    if SUDO_PW:
        return subprocess.run(
            ["sudo", "-S", "bash", "-c", cmd_str],
            input=SUDO_PW + "\n", capture_output=True, text=True, timeout=timeout,
        )
    return subprocess.run(
        ["sudo", "-n", "bash", "-c", cmd_str],
        capture_output=True, text=True, timeout=timeout,
    )


def docker_safe_rm(path):
    assert path.startswith("/dev/shm/bench-")
    return run(["docker", "run", "--rm", "-v", "/dev/shm:/dev/shm",
                "alpine:3", "rm", "-rf", path], timeout=60)


def shard_name(i):    return f"bench-mariadb-{i}"
def shard_subvol(i):  return f"{BTRFS_MNT}/shard-{i}"
def shard_port(i):    return 33880 + i


def sql_shard(i, db, q, timeout=60):
    return run(["mariadb", "-h127.0.0.1", f"-P{shard_port(i)}",
                "-uroot", f"--password={ROOT_PW}", "-BN", db, "-e", q],
               timeout=timeout)


def sql_batch_shard(i, stmts, timeout=120):
    ddl = ";\n".join(stmts) + ";"
    return subprocess.run(
        ["mariadb", "-h127.0.0.1", f"-P{shard_port(i)}",
         "-uroot", f"--password={ROOT_PW}"],
        input=ddl, capture_output=True, text=True, timeout=timeout)


def strip_secondary_indexes(sql_text):
    out_lines, idx_map, current, in_create = [], {}, None, False
    for line in sql_text.split("\n"):
        if not in_create:
            m = re.match(r"\s*CREATE TABLE\s+`?([^`\s(]+)`?\s*\(", line)
            if m:
                current = m.group(1); idx_map[current] = []; in_create = True
            out_lines.append(line); continue
        stripped = line.strip().rstrip(",").rstrip()
        if stripped.startswith(")") and ("ENGINE" in line or "AUTO_INCREMENT" in line or line.strip() == ");"):
            in_create = False; out_lines.append(line); current = None; continue
        m = re.match(r"\s*(UNIQUE\s+)?(KEY|INDEX)\s+`?([^`\s(]+)`?\s*\((.+?)\)\s*,?\s*$", line, re.IGNORECASE)
        if m and not stripped.upper().startswith("PRIMARY"):
            if current is not None:
                idx_map[current].append((m.group(3), m.group(4), bool(m.group(1))))
            continue
        if re.match(r"\s*(CONSTRAINT|FOREIGN\s+KEY)", line, re.IGNORECASE):
            continue
        out_lines.append(line)
    text = "\n".join(out_lines)
    text = re.sub(r",(\s*\n\s*\)\s*ENGINE)", r"\1", text)
    text = re.sub(r",(\s*\n\s*\)\s*;)", r"\1", text)
    return text, idx_map


def setup_btrfs():
    """Create btrfs loopback on /dev/shm, mkfs, mount."""
    sudo(f"umount {BTRFS_MNT} 2>/dev/null; "
         f"rm -f {BTRFS_IMG}; "
         f"truncate -s {BTRFS_SIZE_GB}G {BTRFS_IMG} && "
         f"mkfs.btrfs -q -f {BTRFS_IMG} && "
         f"mkdir -p {BTRFS_MNT} && "
         f"mount -o loop,nodatacow {BTRFS_IMG} {BTRFS_MNT} && "
         f"chmod 0777 {BTRFS_MNT}")


def teardown_btrfs():
    sudo(f"umount {BTRFS_MNT} 2>/dev/null; rm -f {BTRFS_IMG}")


def create_subvol(i):
    sv = shard_subvol(i)
    r = sudo(f"btrfs subvolume delete {sv} 2>/dev/null; "
             f"btrfs subvolume create {sv} && chmod 0777 {sv}")
    if r.returncode:
        print(f"  create subvol {sv} failed: {r.stderr[:200]}"); sys.exit(1)


def snapshot_subvol(src_i, dst_i):
    src = shard_subvol(src_i); dst = shard_subvol(dst_i)
    t0 = time.time()
    r = sudo(f"btrfs subvolume snapshot {src} {dst} && chmod 0777 {dst}")
    return (dst_i, r.returncode == 0, time.time()-t0,
            r.stderr[:200] if r.returncode else "")


def cleanup_all():
    for i in range(SHARDS + 2):
        run(["docker", "rm", "-f", shard_name(i)])
        sudo(f"btrfs subvolume delete {shard_subvol(i)} 2>/dev/null")
    docker_safe_rm("/dev/shm/bench-dump-ci3.sql")
    docker_safe_rm("/dev/shm/bench-dump-stripped.sql")
    docker_safe_rm(BACKUP_DIR)


def start_shard(i):
    datadir = shard_subvol(i)
    r = run([
        "docker", "run", "-d", "--name", shard_name(i),
        "-e", f"MYSQL_ROOT_PASSWORD={ROOT_PW}",
        "-p", f"127.0.0.1:{shard_port(i)}:3306",
        "-v", f"{datadir}:/var/lib/mysql",
        "-v", "/dev/shm:/dev/shm",
        MARIA_IMAGE,
        "--innodb-buffer-pool-size=1G",
        "--innodb-doublewrite=0",
        "--innodb-flush-log-at-trx-commit=0",
        "--innodb-flush-method=O_DIRECT_NO_FSYNC",
        "--skip-log-bin",
        "--max-connections=500",
        "--innodb-temp-data-file-path=ibtmp1:12M:autoextend:max:4G",
    ] + (["--innodb-change-buffering=none"] if MARIA_IMAGE.startswith("mariadb:10") else []))
    if r.returncode:
        return (i, False, f"docker run failed: {r.stderr[:200]}")
    for _ in range(60):
        if sql_shard(i, "", "SELECT 1").returncode == 0:
            return (i, True, None)
        time.sleep(0.5)
    return (i, False, "did not come up")


def stop_shard_clean(i):
    r = run(["docker", "stop", "-t", "30", shard_name(i)], timeout=60)
    run(["docker", "rm", "-f", shard_name(i)])
    return r.returncode == 0


def clone_one_shard0(i):
    target = f"dl_clone_{i}"
    sql_shard(0, "", f"DROP DATABASE IF EXISTS {target}")
    sql_shard(0, "", f"CREATE DATABASE {target}")
    t0 = time.time()
    try:
        r = sql_batch_shard(0, [f"CREATE TABLE {target}.`{t}` LIKE {SRC_DB}.`{t}`"
                                for t in TABLES])
        if r.returncode:
            return (i, False, f"create: {r.stderr[:200]}", time.time()-t0)
        r = sql_batch_shard(0, [f"ALTER TABLE {target}.`{t}` DISCARD TABLESPACE"
                                for t in TABLES])
        if r.returncode:
            return (i, False, f"discard: {r.stderr[:200]}", time.time()-t0)
        cp_parts = [
            f"cp {BACKUP_DIR}/{t}.ibd /var/lib/mysql/{target}/{t}.ibd && "
            f"cp {BACKUP_DIR}/{t}.cfg /var/lib/mysql/{target}/{t}.cfg"
            for t in TABLES
        ]
        cp_full = " && ".join(cp_parts) + f" && chown -R mysql:mysql /var/lib/mysql/{target}/"
        r = run(["docker", "exec", shard_name(0), "sh", "-c", cp_full], timeout=60)
        if r.returncode:
            return (i, False, f"cp: {r.stderr[:200]}", time.time()-t0)
        r = sql_batch_shard(0, [f"ALTER TABLE {target}.`{t}` IMPORT TABLESPACE"
                                for t in TABLES])
        if r.returncode:
            return (i, False, f"import: {r.stderr[:200]}", time.time()-t0)
        add_stmts = []
        for t in TABLES:
            for name, cols, is_uq in INDEXES_PER_TABLE.get(t, []):
                uq = "UNIQUE " if is_uq else ""
                add_stmts.append(
                    f"ALTER TABLE {target}.`{t}` ADD {uq}INDEX `{name}` ({cols})"
                )
        if add_stmts:
            r = sql_batch_shard(0, add_stmts)
            if r.returncode:
                return (i, False, f"addidx: {r.stderr[:200]}", time.time()-t0)
        return (i, True, None, time.time()-t0)
    except Exception as e:
        return (i, False, f"exc: {e}", time.time()-t0)


def main():
    global TABLES, INDEXES_PER_TABLE
    # Verify sudo works (either with SUDO_PW or NOPASSWD).
    if not SUDO_PW:
        check = subprocess.run(["sudo", "-n", "true"], capture_output=True, text=True)
        if check.returncode != 0:
            print("SUDO_PW env var not set and passwordless sudo unavailable. "
                  "Either export SUDO_PW or configure NOPASSWD for the sudo user.")
            sys.exit(1)

    t_total_start = time.time()
    print(f"=== Setup (SHARDS={SHARDS}, N/shard={N_PER_SHARD}, W={W}) ===")
    cleanup_all()

    # === btrfs loopback on /dev/shm ===
    print(f"\n=== Create btrfs-on-/dev/shm loopback ({BTRFS_SIZE_GB} GB) ===")
    t0 = time.time()
    setup_btrfs()
    print(f"  mounted at {BTRFS_MNT} in {time.time()-t0:.1f}s")

    create_subvol(0)
    print(f"  subvol 0 created")

    # === Start shard 0 ===
    print(f"\n=== Start {shard_name(0)} (btrfs subvol on /dev/shm) ===")
    t0 = time.time()
    _, ok, err = start_shard(0)
    if not ok:
        print(f"shard 0 start failed: {err}"); sys.exit(1)
    print(f"  ready in {time.time()-t0:.1f}s")

    # === Stage source ===
    print("\n=== Stage source schema (strip + load) ===")
    t0 = time.time()
    docker_safe_rm("/dev/shm/bench-dump-ci3.sql")
    with open("/dev/shm/bench-dump-ci3.sql", "wb") as f:
        subprocess.run(
            ["docker", "exec", SOURCE_CONTAINER,
             "mariadb-dump", "-uroot", "-proot",
             "--single-transaction", "--quick", "--no-tablespaces",
             "--no-create-db", "--routines", "--triggers", SOURCE_DB],
            stdout=f, stderr=subprocess.PIPE, timeout=120)
    with open("/dev/shm/bench-dump-ci3.sql") as f:
        raw = f.read()
    stripped, INDEXES_PER_TABLE = strip_secondary_indexes(raw)
    with open("/dev/shm/bench-dump-stripped.sql", "w") as f:
        f.write(stripped)
    sql_shard(0, "", f"CREATE DATABASE {SRC_DB}")
    with open("/dev/shm/bench-dump-stripped.sql") as f:
        subprocess.run(
            ["mariadb", "-h127.0.0.1", f"-P{shard_port(0)}",
             "-uroot", f"--password={ROOT_PW}",
             "--init-command=SET sql_log_bin=0; SET autocommit=1;", SRC_DB],
            stdin=f, capture_output=True, text=True, timeout=120)
    r = sql_shard(0, "",
        f"SELECT TABLE_NAME FROM information_schema.TABLES "
        f"WHERE TABLE_SCHEMA='{SRC_DB}' AND TABLE_TYPE='BASE TABLE' "
        f"AND ENGINE='InnoDB' ORDER BY TABLE_NAME")
    TABLES[:] = [t.strip() for t in r.stdout.splitlines() if t.strip()]
    print(f"  loaded source ({len(TABLES)} tables, "
          f"{sum(len(v) for v in INDEXES_PER_TABLE.values())} indexes) "
          f"in {time.time()-t0:.1f}s")

    # === Stage .ibd ===
    print("\n=== Stage .ibd via FLUSH FOR EXPORT ===")
    t0 = time.time()
    run(["mkdir", "-p", BACKUP_DIR]); run(["chmod", "0777", BACKUP_DIR])
    table_list = ", ".join(f"`{t}`" for t in TABLES)
    lock_proc = subprocess.Popen(
        ["mariadb", "-h127.0.0.1", f"-P{shard_port(0)}",
         "-uroot", f"--password={ROOT_PW}", SRC_DB],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    lock_proc.stdin.write(
        f"FLUSH TABLES {table_list} FOR EXPORT;\n"
        f"SELECT /*export_holder*/ SLEEP(300);\n".encode())
    lock_proc.stdin.flush()
    time.sleep(4)
    cp = run(["docker", "exec", shard_name(0), "sh", "-c",
              f"cp /var/lib/mysql/{SRC_DB}/*.ibd /var/lib/mysql/{SRC_DB}/*.cfg "
              f"{BACKUP_DIR}/ && ls {BACKUP_DIR}/ | wc -l"])
    print(f"  staged {cp.stdout.strip()} files in {time.time()-t0:.1f}s")
    sess = sql_shard(0, "",
        "SELECT id FROM information_schema.processlist "
        "WHERE info LIKE '%export_holder%' LIMIT 1")
    if sess.stdout.strip():
        sql_shard(0, "", f"KILL {sess.stdout.strip()}", timeout=5)
    try:
        lock_proc.kill(); lock_proc.wait(timeout=5)
    except Exception:
        pass

    # === Bake ===
    print(f"\n=== BAKE: {N_PER_SHARD} clones on shard 0 (btrfs-on-shm), W={W} ===")
    t0 = time.time()
    errors = []
    with cf.ThreadPoolExecutor(max_workers=W) as ex:
        futures = {ex.submit(clone_one_shard0, i): i for i in range(1, N_PER_SHARD+1)}
        for fut in cf.as_completed(futures):
            i, ok, err, _el = fut.result()
            if not ok:
                errors.append((i, err))
    t_bake = time.time() - t0
    print(f"  baked {N_PER_SHARD - len(errors)}/{N_PER_SHARD} in {t_bake:.1f}s "
          f"({N_PER_SHARD/t_bake*60:.0f} c/min)")
    if errors:
        for i, e in errors[:3]:
            print(f"    err clone_{i}: {e[:120]}")

    # === Stop shard 0 ===
    print("\n=== Stop shard 0 cleanly ===")
    t0 = time.time()
    stop_shard_clean(0)
    print(f"  stopped in {time.time()-t0:.1f}s")

    # === btrfs snapshot to shards 1..S-1 ===
    print(f"\n=== btrfs snapshot shard 0 → shards 1..{SHARDS-1} (parallel W=8) ===")
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda i: snapshot_subvol(0, i), range(1, SHARDS)))
    snap_times = [t for _, _, t, _ in results]
    fails = [(i, e) for i, ok, _, e in results if not ok]
    t_snap_wall = time.time() - t0
    print(f"  snap wall: {t_snap_wall:.2f}s "
          f"(avg per: {sum(snap_times)/len(snap_times)*1000:.0f}ms, "
          f"max: {max(snap_times)*1000:.0f}ms)")
    if fails:
        for i, e in fails[:3]:
            print(f"  snap {i} FAILED: {e}")

    # === Start all shards in parallel ===
    print(f"\n=== Start {SHARDS} mariadbds in parallel (W={START_PARALLEL}) ===")
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=START_PARALLEL) as ex:
        results = list(ex.map(start_shard, range(SHARDS)))
    t_starts = time.time() - t0
    fails = [(i, e) for i, ok, e in results if not ok]
    print(f"  all {SHARDS} shards started in {t_starts:.1f}s "
          f"({len(results) - len(fails)} ok)")
    for i, e in fails[:3]:
        print(f"  shard {i} start FAILED: {e}")

    # === Verify ===
    print(f"\n=== Verify all shards have {N_PER_SHARD} clones ===")
    all_ok = True
    for i in range(SHARDS):
        r = sql_shard(i, "",
            "SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME LIKE 'dl_clone_%'")
        n = r.stdout.strip()
        r2 = sql_shard(i, "dl_clone_1", "SELECT COUNT(*) FROM employees")
        rows = r2.stdout.strip()
        status = "✓" if n == str(N_PER_SHARD) and rows == "1035" else "✗"
        print(f"  {status} shard {i}: {n} pool DBs, dl_clone_1.employees = {rows}")
        if status == "✗":
            all_ok = False

    t_total = time.time() - t_total_start
    pool_total = SHARDS * N_PER_SHARD

    print(f"\n=== RESULTS (SHARDS={SHARDS}, N={N_PER_SHARD}) ===")
    print(f"  Bake on shard 0 ({N_PER_SHARD} clones × W={W}): {t_bake:.1f}s")
    print(f"  btrfs snapshot × {SHARDS-1} (parallel/8): {t_snap_wall*1000:.0f} ms wall")
    print(f"  Start (parallel/{START_PARALLEL}): {t_starts:.1f}s")
    print(f"  ───")
    print(f"  TOTAL wall:        {t_total:.1f}s ({t_total/60:.2f} min)")
    print(f"  Total pool slots:  {pool_total} ({SHARDS} shards × {N_PER_SHARD})")
    print(f"  Effective rate:    {pool_total/t_total*60:.0f} slots/min")
    print(f"  Verification:      {'PASS' if all_ok else 'FAIL'}")

    print(f"\nCompare:")
    print(f"  4-shard btrfs-on-/tmp-loopback (slow disk): 8.2 min")
    print(f"  8-shard tmpfs cp -a: 3.85 min")
    print(f"  THIS (16-shard btrfs-on-/dev/shm + snapshot): {t_total/60:.2f} min")


if __name__ == "__main__":
    try:
        main()
    finally:
        pass  # leave btrfs mounted for inspection; teardown_btrfs() on demand
