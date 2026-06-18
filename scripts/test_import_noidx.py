#!/usr/bin/env python3
"""IMPORT TABLESPACE with pre-stripped indexes — the mysql-fork pattern.

Setup:
  1. Spin bench-mariadb-A.
  2. Apply SOURCE_DB_A SCHEMA stripped of secondary indexes (sed-style).
  3. Load data via dump|load — fast because no index maintenance during INSERTs.
  4. FLUSH TABLES FOR EXPORT, stage .ibd + .cfg (these now have NO sec indexes baked in).
  5. Per clone:
     a. CREATE TABLE LIKE source_db_noidx → schema with PK only
     b. DISCARD TABLESPACE
     c. cp .ibd + .cfg (in-container)
     d. IMPORT TABLESPACE — fast, no index validation
     e. ALTER TABLE ADD INDEX × N — sort-merge index build
  6. Compare per-clone wall to dump|load 15s baseline.
"""
import os, re, sys, subprocess, time

PORT = 33881
ROOT_PW = "root"
SRC_DB = "source_db_noidx"
BACKUP_DIR = "/dev/shm/bench-backup-noidx"


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


def strip_secondary_indexes(sql_text):
    """Remove KEY / UNIQUE KEY clauses from CREATE TABLE blocks, keep PRIMARY KEY.
    Also collect the dropped index defs per table so we can re-add them later.
    Returns (stripped_sql, indexes_per_table) where indexes is dict[table_name → [(name, cols_csv, is_unique)]].
    """
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
            out_lines.append(line)
            continue

        stripped = line.strip().rstrip(",").rstrip()
        # Detect end of CREATE TABLE (the closing paren on its own line or with ENGINE=)
        if stripped.startswith(")") and ("ENGINE" in line or "AUTO_INCREMENT" in line or line.strip() == ");"):
            in_create = False
            out_lines.append(line)
            current_table = None
            continue

        # Match KEY / UNIQUE KEY / INDEX (but NOT PRIMARY KEY or FOREIGN KEY)
        m = re.match(r"\s*(UNIQUE\s+)?(KEY|INDEX)\s+`?([^`\s(]+)`?\s*\((.+?)\)\s*,?\s*$", line, re.IGNORECASE)
        if m and not stripped.upper().startswith("PRIMARY"):
            is_unique = bool(m.group(1))
            idx_name = m.group(3)
            cols = m.group(4)
            if current_table is not None:
                indexes_per_table[current_table].append((idx_name, cols, is_unique))
            # Skip this line — don't emit
            continue

        # Skip CONSTRAINT/FOREIGN KEY lines too (less common in kind-a, but safe)
        if re.match(r"\s*(CONSTRAINT|FOREIGN\s+KEY)", line, re.IGNORECASE):
            continue

        out_lines.append(line)

    # Clean up trailing commas right before the closing paren of CREATE TABLE
    text = "\n".join(out_lines)
    text = re.sub(r",(\s*\n\s*\)\s*ENGINE)", r"\1", text)
    text = re.sub(r",(\s*\n\s*\)\s*;)", r"\1", text)
    return text, indexes_per_table


def main():
    print("=== Cleanup prior + spin bench-mariadb-A ===")
    run(["docker", "rm", "-f", "bench-mariadb-A"])
    for p in ("/dev/shm/bench-ramdisk-a", "/dev/shm/bench-sock-a",
              "/dev/shm/bench-dump-ci3.sql", BACKUP_DIR):
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
        "mariadb:10.6.22",
        "--innodb-buffer-pool-size=1G",
        "--innodb-doublewrite=0",
        "--innodb-flush-log-at-trx-commit=0",
        "--innodb-flush-method=O_DIRECT_NO_FSYNC",
        "--skip-log-bin",
        "--max-connections=500",
        "--innodb-change-buffering=none",
        "--innodb-temp-data-file-path=ibtmp1:12M:autoextend:max:4G",
    ])
    if r.returncode:
        print(f"start failed: {r.stderr}"); sys.exit(1)
    for _ in range(60):
        if sql("", "SELECT 1").returncode == 0:
            break
        time.sleep(0.5)
    print("  mariadb-A ready")

    print("\n=== Get source dump ===")
    t0 = time.time()
    with open("/dev/shm/bench-dump-ci3.sql", "wb") as f:
        subprocess.run(
            ["docker", "exec", "source-mariadb-A",
             "mariadb-dump", "-uroot", "-proot",
             "--single-transaction", "--quick", "--no-tablespaces",
             "--no-create-db", "--routines", "--triggers", "SOURCE_DB_A"],
            stdout=f, stderr=subprocess.PIPE, timeout=120)
    print(f"  dumped {os.path.getsize('/dev/shm/bench-dump-ci3.sql')/1024/1024:.0f} MiB "
          f"in {time.time()-t0:.1f}s")

    # Read + strip indexes
    with open("/dev/shm/bench-dump-ci3.sql") as f:
        dump_raw = f.read()
    stripped, indexes_per_table = strip_secondary_indexes(dump_raw)
    total_idx = sum(len(v) for v in indexes_per_table.values())
    print(f"  stripped {total_idx} secondary indexes from {len(indexes_per_table)} CREATE TABLE blocks")

    with open("/dev/shm/bench-dump-stripped.sql", "w") as f:
        f.write(stripped)
    print(f"  stripped dump: {os.path.getsize('/dev/shm/bench-dump-stripped.sql')/1024/1024:.0f} MiB")

    print("\n=== Load stripped dump into source_db_noidx (no sec indexes) ===")
    sql("", f"CREATE DATABASE {SRC_DB}")
    t0 = time.time()
    with open("/dev/shm/bench-dump-stripped.sql") as f:
        r = subprocess.run(
            ["mariadb", "-h127.0.0.1", f"-P{PORT}",
             "-uroot", f"--password={ROOT_PW}",
             "--init-command=SET sql_log_bin=0; SET autocommit=1;", SRC_DB],
            stdin=f, capture_output=True, text=True, timeout=120)
    print(f"  loaded source_db_noidx in {time.time()-t0:.1f}s "
          f"(should be faster than full-index 15s)")
    if r.returncode:
        print(f"  load stderr: {r.stderr[:300]}")

    # Get table list
    r = sql("", f"SELECT TABLE_NAME FROM information_schema.TABLES "
                f"WHERE TABLE_SCHEMA='{SRC_DB}' AND TABLE_TYPE='BASE TABLE' "
                f"AND ENGINE='InnoDB' ORDER BY TABLE_NAME")
    tables = [t.strip() for t in r.stdout.splitlines() if t.strip()]
    print(f"  source has {len(tables)} InnoDB tables")

    # FLUSH FOR EXPORT
    print("\n=== FLUSH TABLES FOR EXPORT + stage ===")
    table_list = ", ".join(f"`{t}`" for t in tables)
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

    cp_cmd = (
        f"cp /var/lib/mysql/{SRC_DB}/*.ibd /var/lib/mysql/{SRC_DB}/*.cfg "
        f"{BACKUP_DIR}/ && "
        f"ls {BACKUP_DIR}/ | wc -l"
    )
    r = run(["docker", "exec", "bench-mariadb-A", "sh", "-c", cp_cmd])
    print(f"  staged files: {r.stdout.strip()} (expected {len(tables)*2})")

    # Release lock
    sess = sql("",
        "SELECT id FROM information_schema.processlist "
        "WHERE info LIKE '%export_holder%' LIMIT 1")
    if sess.stdout.strip():
        sql("", f"KILL {sess.stdout.strip()}", timeout=5)
    try:
        lock_proc.kill(); lock_proc.wait(timeout=5)
    except Exception:
        pass

    # --- 5 trials of IMPORT TABLESPACE + ADD INDEX ---
    print("\n=== M5: IMPORT TABLESPACE (no-idx source) + ADD INDEX, 5 trials ===")
    timings = []
    for trial in range(1, 6):
        target = f"clone_imp_{trial}"
        sql("", f"DROP DATABASE IF EXISTS {target}")
        sql("", f"CREATE DATABASE {target}")

        t0 = time.time()

        # 1. CREATE TABLE LIKE source_db_noidx → these have NO secondary indexes
        ddl = ";\n".join(f"CREATE TABLE {target}.`{t}` LIKE {SRC_DB}.`{t}`"
                          for t in tables) + ";"
        r = subprocess.run(
            ["mariadb", "-h127.0.0.1", f"-P{PORT}",
             "-uroot", f"--password={ROOT_PW}"],
            input=ddl, capture_output=True, text=True, timeout=120)
        if r.returncode:
            print(f"  trial {trial} CREATE TABLE LIKE failed: {r.stderr[:200]}"); break
        t_create = time.time() - t0

        # 2. DISCARD TABLESPACE
        t1 = time.time()
        ddl = ";\n".join(f"ALTER TABLE {target}.`{t}` DISCARD TABLESPACE"
                          for t in tables) + ";"
        r = subprocess.run(
            ["mariadb", "-h127.0.0.1", f"-P{PORT}",
             "-uroot", f"--password={ROOT_PW}"],
            input=ddl, capture_output=True, text=True, timeout=120)
        if r.returncode:
            print(f"  trial {trial} DISCARD failed: {r.stderr[:200]}"); break
        t_discard = time.time() - t1

        # 3. cp + chown to mysql user (UID 999) — mariadbd needs O_RDWR
        t2 = time.time()
        cp_cmd_parts = [
            f"cp {BACKUP_DIR}/{t}.ibd /var/lib/mysql/{target}/{t}.ibd && "
            f"cp {BACKUP_DIR}/{t}.cfg /var/lib/mysql/{target}/{t}.cfg"
            for t in tables
        ]
        cp_cmd_full = " && ".join(cp_cmd_parts) + (
            f" && chown -R mysql:mysql /var/lib/mysql/{target}/"
        )
        r = run(["docker", "exec", "bench-mariadb-A", "sh", "-c", cp_cmd_full])
        if r.returncode:
            print(f"  trial {trial} cp failed: {r.stderr[:200]}"); break

        # Debug on first trial only: list target dir + check ownership of one file
        if trial == 1:
            d = run(["docker", "exec", "bench-mariadb-A", "sh", "-c",
                     f"ls -la /var/lib/mysql/{target}/ | head -5"])
            print(f"    [debug] target dir sample:\n      " +
                  "\n      ".join(d.stdout.splitlines()[:5]))
        t_cp = time.time() - t2

        # 4. IMPORT TABLESPACE — should be fast, no index validation
        t3 = time.time()
        ddl = ";\n".join(f"ALTER TABLE {target}.`{t}` IMPORT TABLESPACE"
                          for t in tables) + ";"
        r = subprocess.run(
            ["mariadb", "-h127.0.0.1", f"-P{PORT}",
             "-uroot", f"--password={ROOT_PW}"],
            input=ddl, capture_output=True, text=True, timeout=120)
        if r.returncode:
            print(f"  trial {trial} IMPORT failed: {r.stderr[:200]}"); break
        t_import = time.time() - t3

        # 5. ADD INDEX × N — sort-merge index build
        t4 = time.time()
        add_stmts = []
        for t in tables:
            for name, cols, is_uq in indexes_per_table.get(t, []):
                uq = "UNIQUE " if is_uq else ""
                add_stmts.append(
                    f"ALTER TABLE {target}.`{t}` ADD {uq}INDEX `{name}` ({cols})"
                )
        if add_stmts:
            ddl = ";\n".join(add_stmts) + ";"
            r = subprocess.run(
                ["mariadb", "-h127.0.0.1", f"-P{PORT}",
                 "-uroot", f"--password={ROOT_PW}"],
                input=ddl, capture_output=True, text=True, timeout=120)
            if r.returncode:
                print(f"  trial {trial} ADD INDEX failed: {r.stderr[:200]}"); break
        t_addidx = time.time() - t4

        elapsed = time.time() - t0
        timings.append(elapsed)

        ver = sql(target, "SELECT COUNT(*) FROM employees")
        rows = ver.stdout.strip()
        print(f"  trial {trial}: total={elapsed:.2f}s "
              f"[create={t_create:.2f} discard={t_discard:.2f} cp={t_cp:.2f} "
              f"import={t_import:.2f} addidx={t_addidx:.2f}] "
              f"(employees={rows})")

    if timings:
        avg = sum(timings) / len(timings)
        print(f"\n=== Average IMPORT TABLESPACE (no-idx) single-clone: {avg:.2f}s ===")
        print(f"  vs dump|load 15.0s → speedup {15/avg:.2f}×")

    print("\n=== Cleanup ===")
    run(["docker", "rm", "-f", "bench-mariadb-A"])
    for p in ("/dev/shm/bench-ramdisk-a", "/dev/shm/bench-sock-a",
              "/dev/shm/bench-dump-ci3.sql", "/dev/shm/bench-dump-stripped.sql",
              BACKUP_DIR):
        docker_safe_rm(p)


if __name__ == "__main__":
    main()
