#!/usr/bin/env python3
"""IMPORT TABLESPACE (no-idx) at W=8 parallel — does the 2.77× single-clone
speedup hold at concurrency, or does dict_sys.latch eat it?

Setup same as test_import_noidx.py:
  - Source schema stripped of secondary indexes
  - Staged .ibd + .cfg files on /dev/shm
  - Per clone: CREATE TABLE LIKE + DISCARD + cp + IMPORT + ADD INDEX

Compare to:
  dump|load N=20 W=8 (socket, kind-a, single mariadbd) = 116.0s
"""
import os, re, sys, subprocess, time, threading
import concurrent.futures as cf

PORT = 33881
ROOT_PW = "root"
SRC_DB = "source_db_noidx"
BACKUP_DIR = "/dev/shm/bench-backup-noidx"
N = int(os.getenv("N", "20"))
W = int(os.getenv("W", "8"))
MARIA_IMAGE = os.getenv("MARIA_IMAGE", "mariadb:10.6.22")

# Global state populated during setup; workers read it
TABLES = []
INDEXES_PER_TABLE = {}


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def docker_safe_rm(path):
    assert path.startswith("/dev/shm/bench-")
    return run(["docker", "run", "--rm", "-v", "/dev/shm:/dev/shm",
                "alpine:3", "rm", "-rf", path], timeout=30)


def sql(db, q, timeout=60):
    return run(["mariadb", "-h127.0.0.1", f"-P{PORT}",
                "-uroot", f"--password={ROOT_PW}", "-BN", db, "-e", q],
               timeout=timeout)


def sql_batch(stmts, timeout=120):
    ddl = ";\n".join(stmts) + ";"
    return subprocess.run(
        ["mariadb", "-h127.0.0.1", f"-P{PORT}",
         "-uroot", f"--password={ROOT_PW}"],
        input=ddl, capture_output=True, text=True, timeout=timeout)


def strip_secondary_indexes(sql_text):
    out_lines = []
    indexes_per_table = {}
    current_table = None
    in_create = False
    for line in sql_text.split("\n"):
        if not in_create:
            m = re.match(r"\s*CREATE TABLE\s+`?([^`\s(]+)`?\s*\(", line)
            if m:
                current_table = m.group(1)
                indexes_per_table[current_table] = []
                in_create = True
            out_lines.append(line); continue
        stripped = line.strip().rstrip(",").rstrip()
        if stripped.startswith(")") and ("ENGINE" in line or "AUTO_INCREMENT" in line or line.strip() == ");"):
            in_create = False; out_lines.append(line); current_table = None; continue
        m = re.match(r"\s*(UNIQUE\s+)?(KEY|INDEX)\s+`?([^`\s(]+)`?\s*\((.+?)\)\s*,?\s*$", line, re.IGNORECASE)
        if m and not stripped.upper().startswith("PRIMARY"):
            is_unique = bool(m.group(1)); idx_name = m.group(3); cols = m.group(4)
            if current_table is not None:
                indexes_per_table[current_table].append((idx_name, cols, is_unique))
            continue
        if re.match(r"\s*(CONSTRAINT|FOREIGN\s+KEY)", line, re.IGNORECASE):
            continue
        out_lines.append(line)
    text = "\n".join(out_lines)
    text = re.sub(r",(\s*\n\s*\)\s*ENGINE)", r"\1", text)
    text = re.sub(r",(\s*\n\s*\)\s*;)", r"\1", text)
    return text, indexes_per_table


def clone_one(i):
    """Single-clone IMPORT TABLESPACE flow. Returns (i, ok, err, elapsed)."""
    target = f"dl_clone_{i}"
    sql("", f"DROP DATABASE IF EXISTS {target}")
    sql("", f"CREATE DATABASE {target}")
    t0 = time.time()
    try:
        # 1. CREATE TABLE LIKE source_noidx
        r = sql_batch([f"CREATE TABLE {target}.`{t}` LIKE {SRC_DB}.`{t}`"
                       for t in TABLES])
        if r.returncode:
            return (i, False, f"create: {r.stderr[:200]}", time.time()-t0)

        # 2. DISCARD TABLESPACE
        r = sql_batch([f"ALTER TABLE {target}.`{t}` DISCARD TABLESPACE"
                       for t in TABLES])
        if r.returncode:
            return (i, False, f"discard: {r.stderr[:200]}", time.time()-t0)

        # 3. cp + chown (in-container)
        cp_parts = [
            f"cp {BACKUP_DIR}/{t}.ibd /var/lib/mysql/{target}/{t}.ibd && "
            f"cp {BACKUP_DIR}/{t}.cfg /var/lib/mysql/{target}/{t}.cfg"
            for t in TABLES
        ]
        cp_full = " && ".join(cp_parts) + (
            f" && chown -R mysql:mysql /var/lib/mysql/{target}/"
        )
        r = run(["docker", "exec", "bench-mariadb-A", "sh", "-c", cp_full],
                timeout=60)
        if r.returncode:
            return (i, False, f"cp: {r.stderr[:200]}", time.time()-t0)

        # 4. IMPORT TABLESPACE
        r = sql_batch([f"ALTER TABLE {target}.`{t}` IMPORT TABLESPACE"
                       for t in TABLES])
        if r.returncode:
            return (i, False, f"import: {r.stderr[:200]}", time.time()-t0)

        # 5. ADD INDEX × N
        add_stmts = []
        for t in TABLES:
            for name, cols, is_uq in INDEXES_PER_TABLE.get(t, []):
                uq = "UNIQUE " if is_uq else ""
                add_stmts.append(
                    f"ALTER TABLE {target}.`{t}` ADD {uq}INDEX `{name}` ({cols})"
                )
        if add_stmts:
            r = sql_batch(add_stmts)
            if r.returncode:
                return (i, False, f"addidx: {r.stderr[:200]}", time.time()-t0)

        return (i, True, None, time.time()-t0)
    except Exception as e:
        return (i, False, f"exc: {e}", time.time()-t0)


def main():
    global TABLES, INDEXES_PER_TABLE

    print(f"=== Setup (N={N}, W={W}, image={MARIA_IMAGE}) ===")
    run(["docker", "rm", "-f", "bench-mariadb-A"])
    for p in ("/dev/shm/bench-ramdisk-a", "/dev/shm/bench-sock-a",
              "/dev/shm/bench-dump-ci3.sql", "/dev/shm/bench-dump-stripped.sql",
              BACKUP_DIR):
        docker_safe_rm(p)
    for p in ("/dev/shm/bench-ramdisk-a", "/dev/shm/bench-sock-a", BACKUP_DIR):
        run(["mkdir", "-p", p]); run(["chmod", "0777", p])

    r = run([
        "docker", "run", "-d", "--name", "bench-mariadb-A",
        "-e", f"MYSQL_ROOT_PASSWORD={ROOT_PW}",
        "-p", f"127.0.0.1:{PORT}:3306",
        "-v", "/dev/shm/bench-ramdisk-a:/var/lib/mysql",
        "-v", "/dev/shm/bench-sock-a:/run/mysqld",
        "-v", "/dev/shm:/dev/shm",
        MARIA_IMAGE,
        "--innodb-buffer-pool-size=1G",
        "--innodb-doublewrite=0",
        "--innodb-flush-log-at-trx-commit=0",
        "--innodb-flush-method=O_DIRECT_NO_FSYNC",
        "--skip-log-bin",
        "--max-connections=500",
        "--innodb-temp-data-file-path=ibtmp1:12M:autoextend:max:4G",
        # innodb-change-buffering removed in 11.x; only add for 10.6
    ] + (["--innodb-change-buffering=none"] if MARIA_IMAGE.startswith("mariadb:10") else []))
    if r.returncode:
        print(f"start failed: {r.stderr}"); sys.exit(1)
    for _ in range(60):
        if sql("", "SELECT 1").returncode == 0:
            break
        time.sleep(0.5)
    print("  mariadb-A ready")

    # Get dump + strip indexes
    with open("/dev/shm/bench-dump-ci3.sql", "wb") as f:
        subprocess.run(
            ["docker", "exec", "source-mariadb-A",
             "mariadb-dump", "-uroot", "-proot",
             "--single-transaction", "--quick", "--no-tablespaces",
             "--no-create-db", "--routines", "--triggers", "SOURCE_DB_A"],
            stdout=f, stderr=subprocess.PIPE, timeout=120)
    with open("/dev/shm/bench-dump-ci3.sql") as f:
        dump_raw = f.read()
    stripped, indexes_per_table = strip_secondary_indexes(dump_raw)
    INDEXES_PER_TABLE = indexes_per_table
    with open("/dev/shm/bench-dump-stripped.sql", "w") as f:
        f.write(stripped)
    print(f"  stripped {sum(len(v) for v in INDEXES_PER_TABLE.values())} indexes")

    # Load stripped source
    sql("", f"CREATE DATABASE {SRC_DB}")
    with open("/dev/shm/bench-dump-stripped.sql") as f:
        subprocess.run(
            ["mariadb", "-h127.0.0.1", f"-P{PORT}",
             "-uroot", f"--password={ROOT_PW}",
             "--init-command=SET sql_log_bin=0; SET autocommit=1;", SRC_DB],
            stdin=f, capture_output=True, text=True, timeout=120)
    r = sql("", f"SELECT TABLE_NAME FROM information_schema.TABLES "
                f"WHERE TABLE_SCHEMA='{SRC_DB}' AND TABLE_TYPE='BASE TABLE' "
                f"AND ENGINE='InnoDB' ORDER BY TABLE_NAME")
    TABLES[:] = [t.strip() for t in r.stdout.splitlines() if t.strip()]
    print(f"  source has {len(TABLES)} InnoDB tables")

    # FLUSH FOR EXPORT + stage
    table_list = ", ".join(f"`{t}`" for t in TABLES)
    lock_proc = subprocess.Popen(
        ["mariadb", "-h127.0.0.1", f"-P{PORT}",
         "-uroot", f"--password={ROOT_PW}", SRC_DB],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    lock_proc.stdin.write(
        f"FLUSH TABLES {table_list} FOR EXPORT;\n"
        f"SELECT /*export_holder*/ SLEEP(300);\n".encode())
    lock_proc.stdin.flush()
    time.sleep(4)
    cp = run(["docker", "exec", "bench-mariadb-A", "sh", "-c",
              f"cp /var/lib/mysql/{SRC_DB}/*.ibd /var/lib/mysql/{SRC_DB}/*.cfg "
              f"{BACKUP_DIR}/ && ls {BACKUP_DIR}/ | wc -l"])
    print(f"  staged: {cp.stdout.strip()} files")
    sess = sql("",
        "SELECT id FROM information_schema.processlist "
        "WHERE info LIKE '%export_holder%' LIMIT 1")
    if sess.stdout.strip():
        sql("", f"KILL {sess.stdout.strip()}", timeout=5)
    try:
        lock_proc.kill(); lock_proc.wait(timeout=5)
    except Exception:
        pass

    # === Parallel run ===
    print(f"\n=== PARALLEL: N={N} clones × W={W} workers ===")
    t0 = time.time()
    errors = []
    per_clone_times = []
    with cf.ThreadPoolExecutor(max_workers=W) as ex:
        futures = {ex.submit(clone_one, i): i for i in range(1, N+1)}
        done = 0
        for fut in cf.as_completed(futures):
            i, ok, err, el = fut.result()
            per_clone_times.append(el)
            done += 1
            if not ok:
                errors.append((i, err))
            if done % 5 == 0 or done == N:
                wall = time.time() - t0
                print(f"  [{done}/{N}] {wall:.1f}s wall, "
                      f"avg per-clone={sum(per_clone_times)/len(per_clone_times):.2f}s, "
                      f"{done/wall*60:.0f} c/min")
    wall = time.time() - t0
    ok_count = N - len(errors)

    print(f"\n=== RESULTS ===")
    print(f"  Wall: {wall:.2f}s")
    print(f"  OK: {ok_count}/{N}")
    print(f"  Per-clone wall (avg): {wall/N:.2f}s")
    print(f"  Per-clone exec (avg): {sum(per_clone_times)/len(per_clone_times):.2f}s")
    print(f"  Throughput: {N/wall*60:.0f} c/min")
    print(f"\n  Baselines (single mariadbd, N=20):")
    print(f"    IMPORT TABLESPACE (no-idx) single-clone: 5.41s")
    print(f"    dump|load W=8 socket: 116.0s = 10 c/min single-mariadbd")
    print(f"    dump|load W=8 socket per-clone: 5.8s wall")
    if errors:
        for i, e in errors[:3]:
            print(f"  err clone_{i}: {e[:120]}")

    # Cleanup
    print("\n=== Cleanup ===")
    run(["docker", "rm", "-f", "bench-mariadb-A"])
    for p in ("/dev/shm/bench-ramdisk-a", "/dev/shm/bench-sock-a",
              "/dev/shm/bench-dump-ci3.sql", "/dev/shm/bench-dump-stripped.sql",
              BACKUP_DIR):
        docker_safe_rm(p)


if __name__ == "__main__":
    main()
