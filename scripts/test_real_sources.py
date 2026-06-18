#!/usr/bin/env python3
"""v2: Two mariadbds × W parallel bake on the host, using REAL two-kind
sources pulled from mariadb-persist-{ci3,lar}-template containers.

Differences from test_two_mariadbs.py:
- No synthetic build — sources come from production persist-templates
- bench-A bakes kind-a SOURCE_DB_A clones; bench-B bakes kind-b SOURCE_DB_B
- Pre-flight tmpfs projection: measure real source sizes, multiply by N×2,
  add overhead, abort early if would exceed 85% of /dev/shm cap

Safety carried over:
- assert_safe_path / docker_safe_rm — every rm verified, never crosses /dev/shm/bench-
- HARD_SHM_USE_MB ceiling + HARD_RAM_FLOOR_MB → emergency_stop_containers
- SIGTERM/SIGINT handler + atexit + max wall-clock cap
- ibtmp1 cap (4G per mariadbd) — MariaDB 10.6 syntax

Cleanup uses an ephemeral alpine container to handle UID 999-owned files.
"""
import os, sys, subprocess, time, threading, signal, atexit
import concurrent.futures as cf

ROOT_PW = "root"
N_PER_INSTANCE = int(os.getenv("N", "200"))
W = int(os.getenv("W", "14"))
MIN_RAM_AVAIL_MB = int(os.getenv("MIN_RAM", "15000"))
MIN_SHM_FREE_MB = int(os.getenv("MIN_SHM", "15000"))
HARD_SHM_USE_MB = int(os.getenv("HARD_SHM", "65000"))
HARD_RAM_FLOOR_MB = int(os.getenv("HARD_RAM", "10000"))
MAX_WALL_SEC = int(os.getenv("MAX_WALL", "2400"))
PROJECTION_HEADROOM = float(os.getenv("HEADROOM", "0.85"))

INSTANCES = [
    {"name": "bench-mariadb-A", "port": 33881,
     "datadir": "/dev/shm/bench-ramdisk-a", "dump": "/dev/shm/bench-dump-ci3.sql",
     "sock_dir": "/dev/shm/bench-sock-a", "sock": "/dev/shm/bench-sock-a/mysqld.sock",
     "source_container": "source-mariadb-A",
     "source_db": "SOURCE_DB_A"},
    {"name": "bench-mariadb-B", "port": 33882,
     "datadir": "/dev/shm/bench-ramdisk-b", "dump": "/dev/shm/bench-dump-lar.sql",
     "sock_dir": "/dev/shm/bench-sock-b", "sock": "/dev/shm/bench-sock-b/mysqld.sock",
     "source_container": "source-mariadb-B",
     "source_db": "SOURCE_DB_B"},
]

USE_SOCKET = os.getenv("USE_SOCKET", "1") == "1"
BUFFER_POOL = os.getenv("BUFFER_POOL", "1G")

SAFE_PREFIXES = ("/dev/shm/bench-",)
MIN_PATH_DEPTH = 4

stop_sampler = threading.Event()
ram_history = []
shm_history = []
abort_event = threading.Event()


def assert_safe_path(path):
    if not isinstance(path, str) or not path:
        raise ValueError(f"refuse to rm: not a non-empty string: {path!r}")
    norm = os.path.normpath(path)
    if norm != path:
        raise ValueError(f"refuse to rm: non-canonical: {path!r} → {norm!r}")
    if not any(norm.startswith(p) for p in SAFE_PREFIXES):
        raise ValueError(
            f"refuse to rm: {path!r} doesn't start with {SAFE_PREFIXES}"
        )
    if norm.count("/") < MIN_PATH_DEPTH - 1:
        raise ValueError(f"refuse to rm: too shallow: {path!r}")
    if ".." in norm:
        raise ValueError(f"refuse to rm: contains ..: {path!r}")
    if os.path.islink(path):
        raise ValueError(f"refuse to rm: is a symlink: {path!r}")


def docker_safe_rm(path):
    assert_safe_path(path)
    return subprocess.run(
        ["docker", "run", "--rm", "-v", "/dev/shm:/dev/shm",
         "alpine:3", "rm", "-rf", path],
        capture_output=True, timeout=30)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def sql(inst_or_port, db, q, timeout=60):
    """Accepts either an INSTANCES dict (uses socket) or a raw port int (legacy)."""
    if isinstance(inst_or_port, dict) and USE_SOCKET:
        cmd = ["mariadb", "-S", inst_or_port["sock"],
               "-uroot", f"--password={ROOT_PW}", "-BN", db, "-e", q]
    else:
        port = inst_or_port["port"] if isinstance(inst_or_port, dict) else inst_or_port
        cmd = ["mariadb", "-h127.0.0.1", f"-P{port}",
               "-uroot", f"--password={ROOT_PW}", "-BN", db, "-e", q]
    return run(cmd, timeout=timeout)


def ram_avail_mb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 0


def shm_stats_mb():
    r = run(["df", "-B1M", "/dev/shm"])
    for line in r.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4:
            try:
                return int(parts[2].rstrip("M")), int(parts[3].rstrip("M"))
            except ValueError:
                pass
    return 0, 0


def shm_cap_mb():
    used, free = shm_stats_mb()
    return used + free


def sampler_thread():
    while not stop_sampler.is_set():
        ram = ram_avail_mb()
        shm_used, _ = shm_stats_mb()
        ram_history.append(ram)
        shm_history.append(shm_used)
        if shm_used > HARD_SHM_USE_MB:
            print(f"\n!!! ABORT: /dev/shm {shm_used} > HARD {HARD_SHM_USE_MB} MiB",
                  flush=True)
            abort_event.set()
            emergency_stop_containers()
            return
        if ram < HARD_RAM_FLOOR_MB:
            print(f"\n!!! ABORT: RAM avail {ram} < HARD floor {HARD_RAM_FLOOR_MB} MiB",
                  flush=True)
            abort_event.set()
            emergency_stop_containers()
            return
        time.sleep(2)


def emergency_stop_containers():
    for inst in INSTANCES:
        subprocess.run(["docker", "kill", inst["name"]],
                       capture_output=True, timeout=5)
        subprocess.run(["docker", "rm", "-f", inst["name"]],
                       capture_output=True, timeout=10)
        try:
            r1 = docker_safe_rm(inst["datadir"])
            r2 = docker_safe_rm(inst["dump"])
            if r1.returncode or r2.returncode:
                print(f"    [emergency] rm warning rc={r1.returncode},{r2.returncode}",
                      flush=True)
        except ValueError as e:
            print(f"    [emergency] SKIPPED unsafe rm: {e}", flush=True)
    print("    [emergency] containers + /dev/shm cleaned", flush=True)


def signal_handler(signum, frame):
    print(f"\n!!! signal {signum} — cleanup + exit", flush=True)
    stop_sampler.set()
    abort_event.set()
    emergency_stop_containers()
    os._exit(1)


def wait_for_capacity(worker_id):
    waited = 0
    while not abort_event.is_set():
        ram = ram_avail_mb()
        _, shm_free = shm_stats_mb()
        if ram >= MIN_RAM_AVAIL_MB and shm_free >= MIN_SHM_FREE_MB:
            return waited
        time.sleep(1)
        waited += 1
        if waited == 1 or waited % 10 == 0:
            print(f"    [w{worker_id} THROTTLED {waited}s] "
                  f"ram={ram}MiB shm_free={shm_free}MiB", flush=True)
    return waited


def start_instance(inst):
    name, port, datadir = inst["name"], inst["port"], inst["datadir"]
    assert_safe_path(datadir)
    assert_safe_path(inst["sock_dir"])
    docker_safe_rm(datadir)
    docker_safe_rm(inst["sock_dir"])
    subprocess.run(["mkdir", "-p", datadir, inst["sock_dir"]], check=True)
    subprocess.run(["chmod", "0777", datadir, inst["sock_dir"]], check=True)
    r = run([
        "docker", "run", "-d", "--name", name,
        "-e", f"MYSQL_ROOT_PASSWORD={ROOT_PW}",
        "-p", f"127.0.0.1:{port}:3306",
        "-v", f"{datadir}:/var/lib/mysql",
        # Bind-mount the in-container socket dir to host /dev/shm so the host
        # mariadb client can connect via UNIX socket — bypasses TCP stack
        # (~4-5% per-clone speedup, ~10× lower connection latency).
        "-v", f"{inst['sock_dir']}:/run/mysqld",
        "mariadb:10.6.22",
        f"--innodb-buffer-pool-size={BUFFER_POOL}",
        "--innodb-log-file-size=64M",
        "--innodb-doublewrite=0",
        "--innodb-flush-log-at-trx-commit=0",
        "--innodb-flush-method=O_DIRECT_NO_FSYNC",
        "--skip-log-bin",
        "--max-connections=500",
        "--innodb-change-buffering=none",
        "--innodb-temp-data-file-path=ibtmp1:12M:autoextend:max:4G",
    ])
    if r.returncode:
        print(f"start {name} failed: {r.stderr}"); sys.exit(1)
    # Wait for either socket OR TCP — sockets generally come up slightly later
    for _ in range(60):
        if sql(inst, "", "SELECT 1").returncode == 0:
            # Sanity-echo: confirm the startup flags actually applied
            r = sql(inst, "", "SHOW VARIABLES WHERE Variable_name IN "
                              "('innodb_buffer_pool_size','innodb_flush_log_at_trx_commit',"
                              "'innodb_doublewrite','log_bin','autocommit',"
                              "'innodb_change_buffering','innodb_temp_data_file_path')")
            for line in r.stdout.splitlines():
                print(f"    [cfg {name}] {line}")
            return
        time.sleep(0.5)
    print(f"{name} did not come up"); sys.exit(1)


def measure_source_size_mib(inst):
    """Query source DB size from persist-template container via docker exec."""
    r = subprocess.run([
        "docker", "exec", inst["source_container"],
        "mariadb", "-uroot", "-proot", "-BN", "-e",
        f"SELECT ROUND(SUM(DATA_LENGTH+INDEX_LENGTH)/1024/1024, 1) "
        f"FROM information_schema.tables WHERE TABLE_SCHEMA='{inst['source_db']}'"
    ], capture_output=True, text=True, timeout=30)
    if r.returncode:
        print(f"size-query failed for {inst['source_container']}: {r.stderr}")
        sys.exit(1)
    return float(r.stdout.strip())


def preflight_check(sizes_mib, n):
    """Project tmpfs requirement, abort if exceeds headroom."""
    clones_mib = sum(s * n for s in sizes_mib)
    dumps_mib = sum(sizes_mib)  # each dump ~ source size
    overhead_mib = 5000          # ibtmp (capped 4G × 2) + logs + buffer
    expected_peak_mib = clones_mib + dumps_mib + overhead_mib

    cap = shm_cap_mb()
    threshold = cap * PROJECTION_HEADROOM

    print(f"  source sizes: kind-a={sizes_mib[0]:.0f} MiB, "
          f"Lar={sizes_mib[1]:.0f} MiB")
    print(f"  per-kind clones: {n} × {sizes_mib[0]:.0f} = {sizes_mib[0]*n:.0f} MiB, "
          f"{n} × {sizes_mib[1]:.0f} = {sizes_mib[1]*n:.0f} MiB")
    print(f"  projected peak: clones={clones_mib:.0f} + dumps={dumps_mib:.0f} "
          f"+ overhead={overhead_mib} = {expected_peak_mib:.0f} MiB")
    print(f"  /dev/shm cap: {cap} MiB, {PROJECTION_HEADROOM*100:.0f}% threshold = "
          f"{threshold:.0f} MiB")

    if expected_peak_mib > threshold:
        print(f"\n!!! PRE-FLIGHT ABORT: projection {expected_peak_mib:.0f} MiB > "
              f"threshold {threshold:.0f} MiB")
        print(f"   Reduce N: max safe N ≈ "
              f"{int((threshold - dumps_mib - overhead_mib) / sum(sizes_mib))}")
        return False
    print(f"  ✓ projection fits within headroom")
    return True


def pull_source_dump(inst):
    """Use docker exec on persist-template to dump source DB to bench-mariadbd's dump path."""
    src_cont = inst["source_container"]
    src_db = inst["source_db"]
    dump = inst["dump"]
    t0 = time.time()
    with open(dump, "w") as f:
        r = subprocess.run(
            ["docker", "exec", src_cont,
             "mariadb-dump", "-uroot", "-proot",
             "--no-tablespaces", "--single-transaction", "--no-create-db",
             "--quick", "--routines", "--triggers",
             src_db],
            stdout=f, stderr=subprocess.PIPE, timeout=300,
        )
    if r.returncode:
        print(f"dump failed from {src_cont}: {r.stderr[:300]}")
        sys.exit(1)
    print(f"  [{src_cont} → {dump}] {time.time()-t0:.1f}s, "
          f"size {os.path.getsize(dump)/1024/1024:.0f} MiB")


def clone_one(inst, i, worker_id):
    if abort_event.is_set():
        return (i, False, "ABORTED", 0)
    waited = wait_for_capacity(worker_id)
    if abort_event.is_set():
        return (i, False, "ABORTED during throttle", waited)
    dump = inst["dump"]
    db = f"dl_clone_{i}"
    r = sql(inst, "",
            f"CREATE DATABASE IF NOT EXISTS {db} "
            f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
            timeout=30)
    if r.returncode:
        return (i, False, f"createdb: {r.stderr[:200]}", waited)
    # Init-command stack matching user's tested one-liner:
    # - sql_log_bin=0: skip session binlog housekeeping (~4% measured)
    # - autocommit=1: keep default (was wrong with autocommit=0 — mariadb-dump's
    #   --single-transaction is for the DUMP consistency snapshot, not for
    #   wrapping the LOAD; with no explicit COMMIT, EOF rolls back work)
    init_cmd = "SET sql_log_bin=0; SET autocommit=1;"
    if USE_SOCKET:
        load_cmd = ["mariadb", "-S", inst["sock"],
                    "-uroot", f"--password={ROOT_PW}",
                    f"--init-command={init_cmd}", db]
    else:
        load_cmd = ["mariadb", "-h127.0.0.1", f"-P{inst['port']}",
                    "-uroot", f"--password={ROOT_PW}",
                    f"--init-command={init_cmd}", db]
    with open(dump) as f:
        r = subprocess.run(load_cmd, stdin=f, capture_output=True, text=True,
                           timeout=300)
    if r.returncode:
        return (i, False, f"load: {r.stderr[:200]}", waited)
    return (i, True, None, waited)


def bake_instance(inst, n, w):
    print(f"  [{inst['name']}/{inst['source_db']}] start N={n}, W={w}", flush=True)
    t0 = time.time()
    errors = []
    waits = []
    with cf.ThreadPoolExecutor(max_workers=w,
                                thread_name_prefix=inst['name']) as ex:
        futures = {ex.submit(clone_one, inst, i, j%w): i
                   for j, i in enumerate(range(1, n+1))}
        for fut in cf.as_completed(futures):
            i, ok, err, waited = fut.result()
            waits.append(waited)
            if not ok:
                errors.append((i, err))
    el = time.time() - t0
    print(f"  [{inst['name']}] DONE in {el:.1f}s, {n-len(errors)}/{n} ok, "
          f"throttle waits sum={sum(waits)}s", flush=True)
    return el, errors, sum(waits)


def cleanup():
    for inst in INSTANCES:
        run(["docker", "rm", "-f", inst["name"]])
    for inst in INSTANCES:
        try:
            docker_safe_rm(inst["datadir"])
            docker_safe_rm(inst["dump"])
        except ValueError as e:
            print(f"  cleanup SKIPPED unsafe path: {e}")


def main():
    if "--cleanup" in sys.argv:
        cleanup(); print("cleaned"); return

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    atexit.register(emergency_stop_containers)

    wall_timer = threading.Timer(MAX_WALL_SEC, lambda: (
        print(f"\n!!! MAX_WALL {MAX_WALL_SEC}s exceeded", flush=True),
        abort_event.set(), emergency_stop_containers(), os._exit(2)))
    wall_timer.daemon = True; wall_timer.start()

    cleanup()
    sampler = threading.Thread(target=sampler_thread, daemon=True); sampler.start()

    print("=== Pre-flight ===")
    ram = ram_avail_mb(); shm_used, shm_free = shm_stats_mb()
    print(f"  RAM avail: {ram} MiB    /dev/shm used: {shm_used}, free: {shm_free} MiB")

    print("\n=== Measure source sizes from persist-templates ===")
    sizes = [measure_source_size_mib(inst) for inst in INSTANCES]
    print(f"  kind-a ({INSTANCES[0]['source_db']}): {sizes[0]:.1f} MiB")
    print(f"  B ({INSTANCES[1]['source_db']}): {sizes[1]:.1f} MiB")

    print("\n=== Pre-flight tmpfs projection ===")
    if not preflight_check(sizes, N_PER_INSTANCE):
        sys.exit(3)

    print("\n=== Start bench-mariadb-A and bench-mariadb-B ===")
    for inst in INSTANCES:
        start_instance(inst)
        print(f"  {inst['name']} ready on port {inst['port']}")

    print("\n=== Pull source dumps from persist-templates ===")
    for inst in INSTANCES:
        pull_source_dump(inst)

    ram = ram_avail_mb(); shm_used, _ = shm_stats_mb()
    print(f"\n=== Pre-bake: ram_avail={ram} MiB shm_used={shm_used} MiB ===")

    print(f"\n=== PARALLEL BAKE: 2 mariadbds × N={N_PER_INSTANCE} × W={W} ===")
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        fa = ex.submit(bake_instance, INSTANCES[0], N_PER_INSTANCE, W)
        fb = ex.submit(bake_instance, INSTANCES[1], N_PER_INSTANCE, W)
        elA, errA, waitA = fa.result()
        elB, errB, waitB = fb.result()
    wall = time.time() - t0

    stop_sampler.set(); sampler.join(timeout=5)
    peak_shm = max(shm_history) if shm_history else 0
    min_ram = min(ram_history) if ram_history else 0

    print(f"\n=== RESULTS ===")
    print(f"  kind-a (bench-mariadb-A): {elA:.1f}s  "
          f"({N_PER_INSTANCE-len(errA)}/{N_PER_INSTANCE} ok)  throttle={waitA}s")
    print(f"  B (bench-mariadb-B): {elB:.1f}s  "
          f"({N_PER_INSTANCE-len(errB)}/{N_PER_INSTANCE} ok)  throttle={waitB}s")
    print(f"  Combined wall: {wall:.1f}s")
    print(f"  Total clones:  {2*N_PER_INSTANCE}")
    print(f"  Throughput:    {2*N_PER_INSTANCE / wall * 60:.0f} clones/min")
    print(f"  Peak /dev/shm: {peak_shm} MiB")
    print(f"  Min RAM avail: {min_ram} MiB")
    print(f"\n  Phong baseline 1 mariadbd N=24 W=8 178s = 8 clones/min")
    print(f"  Multiplier vs phong: "
          f"{(2*N_PER_INSTANCE/wall) / (24/178):.2f}×")


if __name__ == "__main__":
    try:
        main()
    finally:
        stop_sampler.set()
