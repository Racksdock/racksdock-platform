#!/usr/bin/env python3
"""A100 WORKFLOW TEST ONLY — runs marketplace, static host-agent, miner bootstrap, validator and the user journey on ONE machine
with a single NVIDIA GPU, using the a100-dev profile. Everything real except GPU attestation (dev CA) and the hypervisor (static).

Prerequisite: setup-box.sh ran on this machine. Run as root: /opt/racksdock/venv/bin/python run_a100_test.py
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

VENV_BIN = Path(sys.executable).parent


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", type=Path, default=Path("/etc/racksdock/a100.env"))
    ap.add_argument("--dir", type=Path, default=Path("/var/lib/racksdock-a100-test"))
    ap.add_argument("--duration", type=int, default=600, help="seconds to keep the rental running (in-rental checks every 60 s)")
    ap.add_argument("--iperf", default="127.0.0.1", help="iperf3 server for Q12 (a local iperf3 -s is started by default)")
    a = ap.parse_args()
    base_env = {**os.environ, **load_env(a.env)}
    d = a.dir
    d.mkdir(parents=True, exist_ok=True)
    logs = d / "logs"
    logs.mkdir(exist_ok=True)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    procs: list[subprocess.Popen] = []

    def spawn(name, cmd, env):
        log = open(logs / f"{name}.log", "ab")  # noqa: SIM115
        p = subprocess.Popen(cmd, env={**base_env, **env}, stdout=log, stderr=log)
        procs.append(p)
        print(f"  started {name} (pid {p.pid})")
        return p

    def run(cmd, env=None):
        r = subprocess.run(cmd, env={**base_env, **(env or {})}, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"command failed ({r.returncode}): {' '.join(map(str, cmd))}\n{r.stderr[-2000:]}")
            raise SystemExit(1)
        return r

    common = {"RACKSDOCK_CHAIN": "mock", "RACKSDOCK_CHAIN_FILE": str(d / "chain.json"), "RACKSDOCK_PLATFORM_URL": base}
    try:
        print("1. iperf3 server + marketplace")
        if a.iperf == "127.0.0.1":
            spawn("iperf3", ["iperf3", "-s", "-p", "5201"], {})
        spawn("marketplace", [str(VENV_BIN / "racksdock-marketplace"), "serve", "--host", "127.0.0.1", "--port", str(port)],
              {**common, "RACKSDOCK_DB_URL": f"sqlite:///{d}/marketplace.db", "RACKSDOCK_BASE_URL": base})
        for _ in range(100):
            try:
                httpx.get(f"{base}/v1/instances/types", timeout=2)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        print("2. host-agent in static mode (this machine is the node)")
        spawn("host-agent", [str(VENV_BIN / "racksdock-host-agent")], {**common, "RACKSDOCK_HOST_AGENT_MODE": "static", "RACKSDOCK_PLATFORM_WS": f"ws://127.0.0.1:{port}/v1/hosts/ws",
              "RACKSDOCK_STATE_DIR": str(d / "host-state"), "RACKSDOCK_MINER_HOTKEY": "5FminerA100", "RACKSDOCK_REGION": "lab", "RACKSDOCK_HEARTBEAT_S": "10"})
        time.sleep(2)
        print("3. miner: bootstrap identity (node-agent runs on this box), commit, register")
        seed = d / "host-state" / "host.seed"
        for _ in range(50):
            if seed.exists():
                break
            time.sleep(0.2)
        out = run([str(VENV_BIN / "racksdock-miner"), "bootstrap"], {**common, "RACKSDOCK_HOTKEY": "5FminerA100", "RACKSDOCK_HOST_SEED": str(seed), "RACKSDOCK_STATE_DIR": str(d / "miner-state"), "RACKSDOCK_POLL_S": "1"})
        print("  " + out.stdout.strip().splitlines()[-1])
        print(f"   API: {base}   logs: {logs}")
        print("4. validator with the real SSH runner (admission test ~3 min, then full qualification ~12 min)")
        spawn("validator", [str(VENV_BIN / "racksdock-validator"), "run"], {**common, "RACKSDOCK_STATE_DIR": str(d / "validator-state"), "RACKSDOCK_HOTKEY": "5GvalA100", "RACKSDOCK_RUNNER": "ssh",
              "RACKSDOCK_IPERF_ENDPOINTS": a.iperf, "RACKSDOCK_CANARY_MEAN_S": "600", "RACKSDOCK_CANARY_DEADLINE_S": "300", "RACKSDOCK_INRENTAL_S": "60", "RACKSDOCK_MICROBENCH_S": "120",
              "RACKSDOCK_REACH_S": "15", "RACKSDOCK_TEMPO_S": "300", "RACKSDOCK_LOOP_S": "2"})
        t0 = time.time()
        last_report = 0.0
        vlog = logs / "validator.log"
        seen_errors: set[str] = set()
        while time.time() - t0 < 3600:
            types = httpx.get(f"{base}/v1/instances/types", timeout=5).json()["instance_types"][0]
            if types["availability"] and types["availability"][0]["available"]:
                break
            if time.time() - last_report > 30:
                last_report = time.time()
                nodes = httpx.get(f"{base}/v1/nodes", timeout=5).json()["nodes"]
                ev = httpx.get(f"{base}/v1/evidence", params={"limit": 1000}, timeout=5).json()["records"]
                kinds: dict[str, int] = {}
                for e in ev:
                    kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
                tail = vlog.read_text().strip().splitlines()[-1][-160:] if vlog.exists() and vlog.read_text().strip() else "(no validator log yet)"
                status = nodes[0]["badges"] if nodes else "no node"
                print(f"  [{time.strftime('%H:%M:%S')} +{int(time.time() - t0)}s] node badges={status} evidence={kinds} | validator: {tail}")
                if vlog.exists():
                    for line in vlog.read_text().splitlines():
                        if ("failed" in line or "ERROR" in line) and line not in seen_errors:
                            seen_errors.add(line)
                            print("  validator error: " + line[-300:])
                for p in procs:
                    if p.poll() is not None:
                        print(f"  a process exited with {p.returncode}; see {logs}")
                        return 1
            nodes = httpx.get(f"{base}/v1/nodes", timeout=5).json()["nodes"]
            if nodes and nodes[0].get("badges") is not None and httpx.get(f"{base}/v1/nodes/{nodes[0]['node_id']}/verification", timeout=5).json()["status"] == "quarantined":
                v = httpx.get(f"{base}/v1/nodes/{nodes[0]['node_id']}/verification", timeout=5).json()
                print("  node QUARANTINED; failing checks:", (v.get("qualification") or {}).get("last_failed", {}).get("failing_checks"))
                print("  see", logs / "validator.log", "and the evidence at", f"{base}/v1/evidence?kind=qualification")
                return 1
            time.sleep(5)
        else:
            print("  not qualified within an hour; see logs")
            return 1
        print(f"  qualified in {time.time() - t0:.0f}s: profile {types['profile']} {types['shade_instance_type']}")
        ver = httpx.get(f"{base}/v1/nodes/{types['availability'][0]['node_id']}/verification", timeout=5).json()
        print("  measured:", json.dumps({k: v for k, v in (ver["qualification"]["spec_measured"] or {}).items() if k in ("gpu", "cpu", "memory_gb", "nvme", "network")}, indent=1))
        print("5. user journey with the racksdock CLI")
        home = d / "cli-home"
        cli_env = {**common, "RACKSDOCK_HOME": str(home)}
        cli = str(VENV_BIN / "racksdock")
        run([cli, "auth", "signup", "demo@example.com", "password1", "--api-url", base], cli_env)
        run([cli, "ssh-keys", "generate", "--name", "demo", "--out", str(d / "demo_key")], cli_env)
        cfg = json.loads((home / "config.json").read_text())
        httpx.post(f"{base}/v1/billing/credits/purchase", json={"cents": 100000}, headers={"X-API-KEY": cfg["api_key"]})
        node_id = types["availability"][0]["node_id"]
        iid = run([cli, "instances", "create", "--name", "a100-demo", "--node", node_id, "--os", "ubuntu24.04"], cli_env).stdout.strip().splitlines()[-1]
        for _ in range(240):
            info = httpx.get(f"{base}/v1/instances/{iid}/info", headers={"X-API-KEY": cfg["api_key"]}, timeout=5).json()
            if info["status"] in ("active", "error"):
                break
            time.sleep(0.5)
        print(f"  instance {iid}: status={info['status']} ip={info['ip']}:{info['ssh_port']} attestation={info['attestation']['status']} gpu_ids_match={info['attestation']['gpu_ids_match']}")
        chk = subprocess.run([cli, "ssh", iid, "--check-only"], env={**base_env, **cli_env}, capture_output=True, text=True)
        print("  " + (chk.stderr.strip().splitlines()[-1] if chk.stderr else chk.stdout))
        print(f"  real login:  ssh -i {d}/demo_key -p {info['ssh_port']} ubuntu@{info['ip']}   (or: racksdock ssh {iid})")
        print(f"  console: {base}/  node page: {base}/nodes/{node_id}")
        t_end = time.time() + a.duration
        while time.time() < t_end:
            time.sleep(30)
            ev = httpx.get(f"{base}/v1/evidence", params={"instance_id": iid, "kind": "inrental"}, timeout=5).json()["records"]
            last = ev[-1]["envelope"]["payload"] if ev else None
            print(f"  [{time.strftime('%H:%M:%S')}] in-rental checks: {len(ev)}" + (f" · last: attestation_ok={last['attestation_ok']} reachable={last['reachable']} error={last['error']}" if last else ""))
        run([cli, "instances", "delete", iid], cli_env)
        print("  deleted; validator runs the readiness canary next")
        return 0
    finally:
        for p in procs:
            p.send_signal(signal.SIGINT)
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        print(f"logs in {logs}")


if __name__ == "__main__":
    raise SystemExit(main())
