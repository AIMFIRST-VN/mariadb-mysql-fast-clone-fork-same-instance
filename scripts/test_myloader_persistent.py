#!/usr/bin/env python3
"""v4: myloader test with PERSISTENT mydumper container.

Per-clone cost was dominated by `docker run mydumper/mydumper:latest ...`
startup (~1.5s) — at N=40 that's ~60s of pure container overhead.

Fix: start ONE persistent mydumper container at the top, then use
`docker exec mydumper-runner myloader ...` per clone. Container startup
amortizes to 0 across all clones.

Everything else (assert_safe_path, sockets, ibtmp1 cap, --optimize-keys=AFTER_IMPORT_PER_TABLE,
init-command for sql_log_bin=0, etc) inherited from the previous bench.
"""
import os, sys, subprocess, time, threading, signal, atexit
import concurrent.futures as cf

ROOT_PW = "root"
N_PER_INSTANCE = int(os.getenv("N", "20"))
W = int(os.getenv("W", "8"))
LOADER_THREADS = int(os.getenv("LOADER_THREADS", "4"))
BUFFER_POOL = os.getenv("BUFFER_POOL", "1G")
MIN_RAM_AVAIL_MB = int(os.getenv("MIN_RAM", "15000"))
MIN_SHM_FREE_MB = int(os.getenv("MIN_SHM", "15000"))
HARD_SHM_USE_MB = int(os.getenv("HARD_SHM", "65000"))
HARD_RAM_FLOOR_MB = int(os.getenv("HARD_RAM", "10000"))
MAX_WALL_SEC = int(os.getenv("MAX_WALL", "1800"))

MYDUMPER_IMAGE = "mydumper/mydumper:latest"
MYDUMPER_RUNNER = "bench-mydumper-runner"

INSTANCES = [
    {"name": "bench-mariadb-A", "port": 33881,
     "datadir": "/dev/shm/bench-ramdisk-a",
     "sock_dir": "/dev/shm/bench-sock-a", "sock": "/dev/shm/bench-sock-a/mysqld.sock",
     "dump_dir": "/dev/shm/bench-mydumper-ci3",
     "source_container": "source-mariadb-A",
     "source_ip": None, "source_db": "SOURCE_DB_A"},
    {"name": "bench-mariadb-B", "port": 33882,
     "datadir": "/dev/shm/bench-ramdisk-b",
     "sock_dir": "/dev/shm/bench-sock-b", "sock": "/dev/shm/bench-sock-b/mysqld.sock",
     "dump_dir": "/dev/shm/bench-mydumper-lar",
     "source_container": "source-mariadb-B",
     "source_ip": None, "source_db": "SOURCE_DB_B"},
]

SAFE_PREFIXES = ("/dev/shm/bench-",)
MIN_PATH_DEPTH = 4
stop_sampler = threading.Event()
ram_history, shm_history = [], []
abort_event = threading.Event()


def assert_safe_path(path):
    if not isinstance(path, str) or not path:
        raise ValueError(f"refuse to rm: not non-empty: {path!r}")
    norm = os.path.normpath(path)
    if norm != path:
        raise ValueError(f"refuse to rm: non-canonical: {path!r}")
    if not any(norm.startswith(p) for p in SAFE_PREFIXES):
        raise ValueError(f"refuse to rm: bad prefix: {path!r}")
    if norm.count("/") < MIN_PATH_DEPTH - 1:
        raise ValueError(f"refuse to rm: too shallow: {path!r}")
    if ".." in norm:
        raise ValueError(f"refuse to rm: contains ..: {path!r}")
    if os.path.islink(path):
        raise ValueError(f"refuse to rm: is symlink: {path!r}")


def docker_safe_rm(path):
    assert_safe_path(path)
    return subprocess.run(
        ["docker", "run", "--rm", "-v", "/dev/shm:/dev/shm",
         "alpine:3", "rm", "-rf", path], capture_output=True, timeout=30)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def sql_sock(inst, db, q, timeout=60):
    return run(["mariadb", "-S", inst["sock"], "-uroot", f"--password={ROOT_PW}",
                "-BN", db, "-e", q], timeout=timeout)


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


def sampler_thread():
    while not stop_sampler.is_set():
        ram = ram_avail_mb(); shm_used, _ = shm_stats_mb()
        ram_history.append(ram); shm_history.append(shm_used)
        if shm_used > HARD_SHM_USE_MB:
            print(f"\n!!! ABORT shm > {HARD_SHM_USE_MB}", flush=True)
            abort_event.set(); emergency_stop_containers(); return
        if ram < HARD_RAM_FLOOR_MB:
            print(f"\n!!! ABORT ram < {HARD_RAM_FLOOR_MB}", flush=True)
            abort_event.set(); emergency_stop_containers(); return
        time.sleep(2)


def emergency_stop_containers():
    subprocess.run(["docker", "kill", MYDUMPER_RUNNER], capture_output=True, timeout=5)
    subprocess.run(["docker", "rm", "-f", MYDUMPER_RUNNER], capture_output=True, timeout=10)
    for inst in INSTANCES:
        subprocess.run(["docker", "kill", inst["name"]], capture_output=True, timeout=5)
        subprocess.run(["docker", "rm", "-f", inst["name"]], capture_output=True, timeout=10)
        try:
            docker_safe_rm(inst["datadir"])
            docker_safe_rm(inst["dump_dir"])
            docker_safe_rm(inst["sock_dir"])
        except ValueError as e:
            print(f"    [emergency] SKIPPED: {e}", flush=True)
    print("    [emergency] cleaned", flush=True)


def signal_handler(signum, frame):
    print(f"\n!!! signal {signum} — cleanup", flush=True)
    stop_sampler.set(); abort_event.set()
    emergency_stop_containers(); os._exit(1)


def wait_for_capacity(worker_id):
    waited = 0
    while not abort_event.is_set():
        ram = ram_avail_mb(); _, shm_free = shm_stats_mb()
        if ram >= MIN_RAM_AVAIL_MB and shm_free >= MIN_SHM_FREE_MB:
            return waited
        time.sleep(1); waited += 1
    return waited


def start_persistent_mydumper():
    """Start the mydumper container ONCE, idle indefinitely.
    All subsequent myloader calls go through `docker exec` — no per-call startup."""
    r = run([
        "docker", "run", "-d", "--name", MYDUMPER_RUNNER,
        "--network", "host",
        "-v", "/dev/shm:/dev/shm",
        "--entrypoint", "/bin/sleep",
        MYDUMPER_IMAGE, "infinity",
    ])
    if r.returncode:
        print(f"start mydumper runner failed: {r.stderr}"); sys.exit(1)
    # Verify myloader is in the container
    r = run(["docker", "exec", MYDUMPER_RUNNER, "myloader", "--version"])
    if r.returncode:
        print(f"myloader missing in container: {r.stderr}"); sys.exit(1)
    print(f"  {MYDUMPER_RUNNER}: {r.stdout.strip()}")


def start_instance(inst):
    name, port, datadir = inst["name"], inst["port"], inst["datadir"]
    assert_safe_path(datadir); assert_safe_path(inst["sock_dir"])
    docker_safe_rm(datadir); docker_safe_rm(inst["sock_dir"])
    subprocess.run(["mkdir", "-p", datadir, inst["sock_dir"]], check=True)
    subprocess.run(["chmod", "0777", datadir, inst["sock_dir"]], check=True)
    r = run([
        "docker", "run", "-d", "--name", name,
        "-e", f"MYSQL_ROOT_PASSWORD={ROOT_PW}",
        "-p", f"127.0.0.1:{port}:3306",
        "-v", f"{datadir}:/var/lib/mysql",
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
    for _ in range(60):
        if sql_sock(inst, "", "SELECT 1").returncode == 0:
            return
        time.sleep(0.5)
    print(f"{name} did not come up"); sys.exit(1)


def get_source_ip(inst):
    r = run(["docker", "inspect", inst["source_container"],
             "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"])
    return r.stdout.strip()


def mydumper_source(inst):
    """Run mydumper INSIDE the persistent runner via docker exec — no startup."""
    src_ip = inst["source_ip"]
    src_db = inst["source_db"]
    outdir = inst["dump_dir"]
    docker_safe_rm(outdir)
    subprocess.run(["mkdir", "-p", outdir], check=True)
    subprocess.run(["chmod", "0777", outdir], check=True)
    t0 = time.time()
    r = run([
        "docker", "exec", MYDUMPER_RUNNER,
        "mydumper",
        "-h", src_ip, "-P", "3306",
        "-u", "root", f"--password={ROOT_PW}",
        "-B", src_db,
        "-o", outdir,
        "-t", "4",
        "--sync-thread-lock-mode=NO_LOCK",
        "--rows", "100000",
        "-c",
    ], timeout=300)
    if r.returncode:
        print(f"mydumper {src_db} failed: {r.stderr[:300]}"); sys.exit(1)
    files = run(["sh", "-c", f"ls {outdir} | wc -l"])
    size = run(["du", "-sh", outdir])
    print(f"  [mydumper {src_db}] {time.time()-t0:.1f}s, "
          f"files={files.stdout.strip()}, {size.stdout.strip()}")


def clone_one(inst, i, worker_id):
    if abort_event.is_set():
        return (i, False, "ABORTED", 0)
    waited = wait_for_capacity(worker_id)
    if abort_event.is_set():
        return (i, False, "ABORTED throttle", waited)

    db = f"dl_clone_{i}"
    # Pre-create target db
    r = sql_sock(inst, "",
                 f"CREATE DATABASE IF NOT EXISTS {db} "
                 f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
                 timeout=30)
    if r.returncode:
        return (i, False, f"createdb: {r.stderr[:200]}", waited)

    # myloader via docker exec in persistent runner
    r = run([
        "docker", "exec", MYDUMPER_RUNNER,
        "myloader",
        "-h", "127.0.0.1", "-P", str(inst["port"]),
        "-u", "root", f"--password={ROOT_PW}",
        "-s", inst["source_db"],
        "-B", db,
        "-d", inst["dump_dir"],
        "-t", str(LOADER_THREADS),
        "--optimize-keys=AFTER_IMPORT_PER_TABLE",
    ], timeout=600)
    if r.returncode != 0:
        return (i, False, f"myloader: {r.stderr[:200]}", waited)
    return (i, True, None, waited)


def bake_instance(inst, n, w):
    print(f"  [{inst['name']}/{inst['source_db']}] start N={n} W={w}", flush=True)
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
    print(f"  [{inst['name']}] DONE {el:.1f}s, {n-len(errors)}/{n} ok", flush=True)
    return el, errors, sum(waits)


def cleanup():
    emergency_stop_containers()


def main():
    if "--cleanup" in sys.argv:
        cleanup(); print("cleaned"); return

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    atexit.register(emergency_stop_containers)

    wall_timer = threading.Timer(MAX_WALL_SEC, lambda: (
        print(f"\n!!! MAX_WALL exceeded", flush=True),
        abort_event.set(), emergency_stop_containers(), os._exit(2)))
    wall_timer.daemon = True; wall_timer.start()

    cleanup()
    sampler = threading.Thread(target=sampler_thread, daemon=True); sampler.start()

    print("=== Pre-flight ===")
    ram = ram_avail_mb(); shm_used, shm_free = shm_stats_mb()
    print(f"  RAM={ram} MiB, shm_used={shm_used}, shm_free={shm_free}")

    print("\n=== Start persistent mydumper runner ===")
    start_persistent_mydumper()

    print("\n=== Resolve persist-template IPs ===")
    for inst in INSTANCES:
        inst["source_ip"] = get_source_ip(inst)
        print(f"  {inst['source_container']} → {inst['source_ip']}")

    print("\n=== Start bench mariadbds (sockets, BP={}) ===".format(BUFFER_POOL))
    for inst in INSTANCES:
        start_instance(inst)
        print(f"  {inst['name']} ready")

    print("\n=== mydumper sources (via docker exec) ===")
    for inst in INSTANCES:
        mydumper_source(inst)

    ram = ram_avail_mb(); shm_used, _ = shm_stats_mb()
    print(f"\n=== Pre-bake: ram={ram} shm_used={shm_used} ===")

    print(f"\n=== PARALLEL BAKE: 2 × mariadbds × N={N_PER_INSTANCE} × "
          f"W={W} × {LOADER_THREADS} myloader-threads ===")
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
    print(f"  kind-a (A): {elA:.1f}s ({N_PER_INSTANCE-len(errA)}/{N_PER_INSTANCE} ok)")
    print(f"  B (B): {elB:.1f}s ({N_PER_INSTANCE-len(errB)}/{N_PER_INSTANCE} ok)")
    print(f"  Combined wall: {wall:.1f}s")
    print(f"  Throughput: {2*N_PER_INSTANCE/wall*60:.0f} clones/min")
    print(f"  Peak /dev/shm: {peak_shm} MiB")
    print(f"  Min RAM: {min_ram} MiB")
    print(f"\nBaselines on the host (N=20):")
    print(f"  dump|load TCP:    150.5s = 16/min")
    print(f"  dump|load Socket: 146.6s = 16/min")
    print(f"  myloader (docker run per clone, prior): 222.7s = 11/min")


if __name__ == "__main__":
    try:
        main()
    finally:
        stop_sampler.set()
