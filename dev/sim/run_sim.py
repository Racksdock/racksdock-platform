#!/usr/bin/env python3
"""Run the whole subnet locally in simulation mode as separate processes, then drive the user journey with the racksdock CLI."""
from __future__ import annotations

import argparse
import json
import os
import shutil
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", type=int, default=2)
    ap.add_argument("--duration", type=int, default=120, help="seconds to keep running after the user journey (0 = exit immediately)")
    ap.add_argument("--dir", type=Path, default=Path("/tmp/racksdock-sim"))
    ap.add_argument("--keep", action="store_true", help="keep the sim directory")
    a = ap.parse_args()
    d = a.dir
    if d.exists() and not a.keep:
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    logs = d / "logs"
    logs.mkdir(exist_ok=True)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    ca_dir = d / "ca"
    procs: list[subprocess.Popen] = []

    def run(cmd, **kw):
        r = subprocess.run(cmd, capture_output=True, text=True, **kw)
        if r.returncode != 0:
            print(f"command failed ({r.returncode}): {' '.join(str(c) for c in cmd)}\n{r.stderr[-2000:]}")
            raise SystemExit(1)
        return r

    def spawn(name, cmd, env):
        log = open(logs / f"{name}.log", "ab")
        p = subprocess.Popen(cmd, env={**os.environ, **env}, stdout=log, stderr=log)
        procs.append(p)
        print(f"  started {name} (pid {p.pid})")
        return p

    common = {"RACKSDOCK_TRUST_MODE": "dev", "RACKSDOCK_DEV_CA_DIR": str(ca_dir), "RACKSDOCK_CHAIN": "mock", "RACKSDOCK_CHAIN_FILE": str(d / "chain.json"), "RACKSDOCK_PLATFORM_URL": base}
    try:
        print("1. dev CA + simulated nodes")
        run([str(VENV_BIN / "racksdock-verify"), "devca", "init", str(ca_dir)])
        for i in range(a.nodes):
            run([str(VENV_BIN / "racksdock-verify"), "devca", "issue-node", str(ca_dir), str(d / f"node{i}")])
        print("2. marketplace")
        spawn("marketplace", [str(VENV_BIN / "racksdock-marketplace"), "serve", "--host", "127.0.0.1", "--port", str(port)],
              {**common, "RACKSDOCK_DB_URL": f"sqlite:///{d}/marketplace.db", "RACKSDOCK_BASE_URL": base})
        for _ in range(100):
            try:
                httpx.get(f"{base}/v1/instances/types", timeout=2)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        print("3. host-agents (one per simulated server)")
        for i in range(a.nodes):
            res = {"gpu_pci": [f"0000:{17 + j:02x}:00.0" for j in range(8)], "nvswitch_pci": [], "nvme_pci": ["0000:a0:00.0"], "vcpus": 120, "memory_gb": 1850, "os_disk_gb": 200}
            (d / f"node{i}" / "resources.json").write_text(json.dumps(res))
            spawn(f"host{i}", [str(VENV_BIN / "racksdock-host-agent")], {**common, "RACKSDOCK_HOST_AGENT_MODE": "sim", "RACKSDOCK_PLATFORM_WS": f"ws://127.0.0.1:{port}/v1/hosts/ws",
                  "RACKSDOCK_STATE_DIR": str(d / f"node{i}" / "host-state"), "RACKSDOCK_MINER_HOTKEY": f"5Fminer{i}", "RACKSDOCK_REGION": ["eu-north-1", "us-west-2"][i % 2],
                  "RACKSDOCK_VM_DIR": str(d / f"node{i}" / "vms"), "RACKSDOCK_SIM_NODE_DIR": str(d / f"node{i}"), "RACKSDOCK_RESOURCES_JSON": str(d / f"node{i}" / "resources.json"), "RACKSDOCK_HEARTBEAT_S": "5"})
        time.sleep(2)
        print("4. miners: bootstrap identity, commit on chain, register")
        for i in range(a.nodes):
            seed = d / f"node{i}" / "host-state" / "host.seed"
            for _ in range(50):
                if seed.exists():
                    break
                time.sleep(0.2)
            out = run([str(VENV_BIN / "racksdock-miner"), "bootstrap"], env={**os.environ, **common, "RACKSDOCK_HOTKEY": f"5Fminer{i}", "RACKSDOCK_HOST_SEED": str(seed), "RACKSDOCK_STATE_DIR": str(d / f"node{i}" / "miner-state"), "RACKSDOCK_POLL_S": "0.5"})
            print("  " + out.stdout.strip().splitlines()[-1])
        print("5. validator (fast cadence)")
        spawn("validator", [str(VENV_BIN / "racksdock-validator"), "run"], {**common, "RACKSDOCK_STATE_DIR": str(d / "validator-state"), "RACKSDOCK_HOTKEY": "5Gvalidator", "RACKSDOCK_RUNNER": "sim",
              "RACKSDOCK_CANARY_MEAN_S": "20", "RACKSDOCK_CANARY_DEADLINE_S": "60", "RACKSDOCK_INRENTAL_S": "10", "RACKSDOCK_MICROBENCH_S": "30", "RACKSDOCK_REACH_S": "5", "RACKSDOCK_TEMPO_S": "60", "RACKSDOCK_LOOP_S": "1"})
        print("6. waiting for qualification ...")
        t0 = time.time()
        while time.time() - t0 < 300:
            types = httpx.get(f"{base}/v1/instances/types", timeout=5).json()["instance_types"][0]
            if len(types["availability"]) >= a.nodes and all(x["available"] for x in types["availability"]):
                break
            time.sleep(2)
        else:
            print("  nodes did not qualify in time; see logs"); return 1
        print(f"  {len(types['availability'])} nodes qualified in {time.time() - t0:.0f}s")
        print("7. user journey with the racksdock CLI")
        home = d / "cli-home"
        cli_env = {**os.environ, **common, "RACKSDOCK_HOME": str(home)}
        racksdock = str(VENV_BIN / "racksdock")
        run([racksdock, "auth", "signup", "demo@example.com", "password1", "--api-url", base], env=cli_env)
        run([racksdock, "ssh-keys", "generate", "--name", "demo", "--out", str(d / "demo_key")], env=cli_env)
        cfg = json.loads((home / "config.json").read_text())
        httpx.post(f"{base}/v1/billing/credits/purchase", json={"cents": 100000}, headers={"X-API-KEY": cfg["api_key"]})
        node_id = types["availability"][0]["node_id"]
        iid = run([racksdock, "instances", "create", "--name", "demo-1", "--node", node_id, "--os", "ubuntu24.04_cuda13.x", "--env", "WANDB_MODE=offline", "--docker", "vllm/vllm-openai:v0.1", "--port", "8000:8000"], env=cli_env).stdout.strip().splitlines()[-1]
        print(f"  created instance {iid}")
        for _ in range(120):
            info = httpx.get(f"{base}/v1/instances/{iid}/info", headers={"X-API-KEY": cfg["api_key"]}, timeout=5).json()
            if info["status"] in ("active", "error"):
                break
            time.sleep(0.5)
        print(f"  status={info['status']} ip={info['ip']}:{info['ssh_port']} attestation={info['attestation']['status']} gpu_ids_match={info['attestation']['gpu_ids_match']}")
        chk = subprocess.run([racksdock, "ssh", iid, "--check-only"], env=cli_env, capture_output=True, text=True)
        print("  " + chk.stderr.strip().splitlines()[-1] if chk.stderr else chk.stdout)
        print(f"  console: {base}/  (login demo@example.com / password1)   node page: {base}/nodes/{node_id}")
        t_end = time.time() + a.duration
        while time.time() < t_end:
            time.sleep(15)
            nodes = httpx.get(f"{base}/v1/nodes", timeout=5).json()["nodes"]
            ev = httpx.get(f"{base}/v1/evidence", params={"limit": 1000}, timeout=5).json()["records"]
            kinds = {}
            for e in ev:
                kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
            print(f"  [{time.strftime('%H:%M:%S')}] nodes: " + ", ".join(f"{n['provider']}={'avail' if n['available'] else 'busy'}" for n in nodes) + f" · evidence: {kinds}")
        run([racksdock, "instances", "delete", iid], env=cli_env)
        print("  deleted instance; validator will run the readiness canary")
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
