#!/usr/bin/env python3
"""Bare-metal VM workflow test on ONE machine: marketplace, host-agent in REAL mode (libvirt/KVM, VFIO passthrough, NAT),
miner bootstrap, validator with the real SSH runner, and the tenant journey (CLI create -> attestation-verified SSH -> delete).
Everything is the production path except GPU attestation when the profile needs the dev CA (GPUs without device attestation).

Prerequisites (as root): host-image/scripts/setup-host.sh ran; dev/vmtest/build-images.sh built `verify` and the tenant OS.
Run as root:  /opt/racksdock/venv/bin/python run_vm_test.py [--os ubuntu24.04] [--duration 600] [--iperf <ip>]
"""
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


def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception as e:
        return f"({e})"


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)  # progress must show when piped through tee
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", type=Path, default=Path("/etc/racksdock/host-agent.env"), help="host-agent env written by setup-host.sh")
    ap.add_argument("--dir", type=Path, default=Path("/var/lib/racksdock-vmtest"))
    ap.add_argument("--os", default="ubuntu24.04", help="tenant image (must be built on this host)")
    ap.add_argument("--duration", type=int, default=600, help="seconds to keep the tenant rental running (in-rental checks every 60 s)")
    ap.add_argument("--iperf", default="", help="iperf3 server for Q12; default: this host's address, with a local iperf3 -s")
    ap.add_argument("--qual-timeout", type=int, default=7200, help="seconds to wait for admission + qualification")
    a = ap.parse_args()
    if os.geteuid() != 0:
        print("run as root (libvirt, iptables and sysfs need it)")
        return 1
    if not a.env.exists():
        print(f"{a.env} missing: run racksdock-node/host-image/scripts/setup-host.sh first")
        return 1
    base_env = {**os.environ, **load_env(a.env)}
    image_dir = Path(base_env.get("RACKSDOCK_IMAGE_DIR", "/var/lib/racksdock/images"))
    for f in ("manifest.signed.json", "catalog.pub", "catalog.json"):
        if not (image_dir / f).exists():
            print(f"{image_dir / f} missing: run dev/vmtest/build-images.sh verify {a.os} first")
            return 1
    catalog = json.loads((image_dir / "catalog.json").read_text())
    built = {i["os"] for i in catalog["images"]}
    if "verify" not in built or a.os not in built:
        print(f"images built on this host: {sorted(built)}; need 'verify' and '{a.os}'")
        return 1
    host_ip = base_env.get("RACKSDOCK_PUBLIC_IP") or sh(["hostname", "-I"]).split()[0]
    iperf = a.iperf or host_ip
    if iperf == host_ip and shutil.which("iperf3") is None:
        print("iperf3 is not installed on this host (Q12 needs an endpoint): apt-get install -y iperf3, or pass --iperf <server>")
        return 1
    for tool in ("virsh", "iptables", "qemu-img"):
        if shutil.which(tool) is None:
            print(f"{tool} not found: rerun setup-host.sh")
            return 1
    d = a.dir
    d.mkdir(parents=True, exist_ok=True)
    logs = d / "logs"
    logs.mkdir(exist_ok=True)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    procs: list[subprocess.Popen] = []

    def spawn(name, cmd, env):
        log = open(logs / f"{name}.log", "ab")
        p = subprocess.Popen(cmd, env={**base_env, **env}, stdout=log, stderr=log)
        procs.append(p)
        print(f"  started {name} (pid {p.pid}) -> {logs / (name + '.log')}")
        return p

    def run(cmd, env=None):
        r = subprocess.run(cmd, env={**base_env, **(env or {})}, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"command failed ({r.returncode}): {' '.join(map(str, cmd))}\n{r.stderr[-3000:]}")
            raise SystemExit(1)
        return r

    def tail(name, n=160):
        p = logs / f"{name}.log"
        if not p.exists() or not p.read_text().strip():
            return "(no log yet)"
        return p.read_text().strip().splitlines()[-1][-n:]

    common = {"RACKSDOCK_CHAIN": "mock", "RACKSDOCK_CHAIN_FILE": str(d / "chain.json"), "RACKSDOCK_PLATFORM_URL": base}
    print(f"profile={base_env.get('RACKSDOCK_PROFILE')} trust={base_env.get('RACKSDOCK_TRUST_MODE')} host_ip={host_ip} images={sorted(built)}")
    try:
        print("1. iperf3 server + marketplace (offers only the images built on this host)")
        if iperf == host_ip:
            spawn("iperf3", ["iperf3", "-s", "-p", "5201"], {})
        spawn("marketplace", [str(VENV_BIN / "racksdock-marketplace"), "serve", "--host", "127.0.0.1", "--port", str(port)],
              {**common, "RACKSDOCK_DB_URL": f"sqlite:///{d}/marketplace.db", "RACKSDOCK_BASE_URL": base, "RACKSDOCK_CATALOG_MANIFEST": str(image_dir / "catalog.json")})
        for _ in range(100):
            try:
                httpx.get(f"{base}/v1/instances/types", timeout=2)
                break
            except Exception:
                time.sleep(0.2)
        print("2. host-agent in REAL mode (libvirt/KVM; stale racksdock VMs are destroyed first; preflight problems appear in its log)")
        spawn("host-agent", [str(VENV_BIN / "racksdock-host-agent")], {**common, "RACKSDOCK_HOST_AGENT_MODE": "real", "RACKSDOCK_PLATFORM_WS": f"ws://127.0.0.1:{port}/v1/hosts/ws",
              "RACKSDOCK_STATE_DIR": str(d / "host-state"), "RACKSDOCK_HEARTBEAT_S": "10"})
        time.sleep(3)
        for line in (logs / "host-agent.log").read_text().splitlines():
            if "preflight" in line or "stale" in line:
                print("  " + line[-220:])
        print("3. miner: bootstrap identity (boots a verify VM, reads its evidence; ~3-6 min on real hardware), commit, register")
        seed = d / "host-state" / "host.seed"
        for _ in range(50):
            if seed.exists():
                break
            time.sleep(0.2)
        t0 = time.time()
        proc = subprocess.Popen([str(VENV_BIN / "racksdock-miner"), "bootstrap"], env={**base_env, **common, "RACKSDOCK_HOTKEY": base_env.get("RACKSDOCK_MINER_HOTKEY", "5FminerTest"),
                                "RACKSDOCK_HOST_SEED": str(seed), "RACKSDOCK_STATE_DIR": str(d / "miner-state"), "RACKSDOCK_POLL_S": "2"},
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        while proc.poll() is None:
            time.sleep(15)
            print(f"  [{time.strftime('%H:%M:%S')} +{int(time.time() - t0)}s] VMs: {sh(['virsh', 'list', '--name']).replace(chr(10), ' ') or '-'} | host-agent: {tail('host-agent')}")
        out = proc.stdout.read() if proc.stdout else ""
        if proc.returncode != 0:
            print("  bootstrap failed:\n" + out[-3000:])
            print(f"  see {logs / 'host-agent.log'}; a VM console: virsh console <name>  (Ctrl+] to leave)")
            return 1
        print("  " + (out.strip().splitlines()[-1] if out.strip() else "registered"))
        print(f"   API: {base}   logs: {logs}")
        print("4. validator with the real SSH runner (admission test, then full qualification; VM boots take minutes each)")
        spawn("validator", [str(VENV_BIN / "racksdock-validator"), "run"], {**common, "RACKSDOCK_STATE_DIR": str(d / "validator-state"), "RACKSDOCK_HOTKEY": "5GvalTest", "RACKSDOCK_RUNNER": "ssh",
              "RACKSDOCK_IPERF_ENDPOINTS": iperf, "RACKSDOCK_CANARY_MEAN_S": "1800", "RACKSDOCK_CANARY_DEADLINE_S": "900", "RACKSDOCK_INRENTAL_S": "60", "RACKSDOCK_MICROBENCH_S": "120",
              "RACKSDOCK_REACH_S": "15", "RACKSDOCK_TEMPO_S": "300", "RACKSDOCK_LOOP_S": "2"})
        t0 = time.time()
        last_report = 0.0
        vlog = logs / "validator.log"
        seen_errors: set[str] = set()
        while time.time() - t0 < a.qual_timeout:
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
                status = nodes[0]["badges"] if nodes else "no node"
                print(f"  [{time.strftime('%H:%M:%S')} +{int(time.time() - t0)}s] badges={status} evidence={kinds} VMs: {sh(['virsh', 'list', '--name']).replace(chr(10), ' ') or '-'}")
                print(f"      validator:  {tail('validator')}")
                print(f"      host-agent: {tail('host-agent')}")
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
            print("  not qualified within the time limit; see logs")
            return 1
        print(f"  qualified in {time.time() - t0:.0f}s: profile {types['profile']} {types['shade_instance_type']}")
        ver = httpx.get(f"{base}/v1/nodes/{types['availability'][0]['node_id']}/verification", timeout=5).json()
        print("  measured:", json.dumps({k: v for k, v in (ver["qualification"]["spec_measured"] or {}).items() if k in ("gpu", "cpu", "memory_gb", "nvme", "network")}, indent=1))
        print(f"5. tenant journey with the racksdock CLI (OS {a.os})")
        home = d / "cli-home"
        cli_env = {**common, "RACKSDOCK_HOME": str(home)}
        cli = str(VENV_BIN / "racksdock")
        run([cli, "auth", "signup", "demo@example.com", "password1", "--api-url", base], cli_env)
        run([cli, "ssh-keys", "generate", "--name", "demo", "--out", str(d / "demo_key")], cli_env)
        cfg = json.loads((home / "config.json").read_text())
        httpx.post(f"{base}/v1/billing/credits/purchase", json={"cents": 100000}, headers={"X-API-KEY": cfg["api_key"]})
        node_id = types["availability"][0]["node_id"]
        iid = run([cli, "instances", "create", "--name", "vm-demo", "--node", node_id, "--os", a.os], cli_env).stdout.strip().splitlines()[-1]
        t0 = time.time()
        info = {"status": "creating"}
        while time.time() - t0 < 1200:
            info = httpx.get(f"{base}/v1/instances/{iid}/info", headers={"X-API-KEY": cfg["api_key"]}, timeout=5).json()
            if info["status"] in ("active", "error"):
                break
            time.sleep(2)
        print(f"  instance {iid}: status={info['status']} ip={info.get('ip')}:{info.get('ssh_port')} attestation={info['attestation']['status']} gpu_ids_match={info['attestation']['gpu_ids_match']}")
        if info["status"] != "active":
            print(f"  not active: {info.get('status_details')}; see {logs}")
            return 1
        chk = subprocess.run([cli, "ssh", iid, "--check-only"], env={**base_env, **cli_env}, capture_output=True, text=True)
        print("  " + (chk.stderr.strip().splitlines()[-1] if chk.stderr else chk.stdout))
        gpu = subprocess.run(["ssh", "-i", str(d / "demo_key"), "-p", str(info["ssh_port"]), "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                              "-o", "ConnectTimeout=20", f"ubuntu@{info['ip']}", "nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader; nproc; free -g | head -2; lsblk -d -o NAME,SIZE | grep -v loop"],
                             capture_output=True, text=True)
        print("  inside the tenant VM:\n    " + (gpu.stdout.strip() or gpu.stderr.strip()[-500:]).replace("\n", "\n    "))
        print(f"  real login:  ssh -i {d}/demo_key -p {info['ssh_port']} ubuntu@{info['ip']}   (or: racksdock ssh {iid})")
        print(f"  host view:   virsh list; virsh dominfo racksdock-{iid}; iptables -t nat -S | grep racksdock-nat")
        print(f"  console: {base}/  node page: {base}/nodes/{node_id}")
        t_end = time.time() + a.duration
        while time.time() < t_end:
            time.sleep(30)
            ev = httpx.get(f"{base}/v1/evidence", params={"instance_id": iid, "kind": "inrental"}, timeout=5).json()["records"]
            last = ev[-1]["envelope"]["payload"] if ev else None
            print(f"  [{time.strftime('%H:%M:%S')}] in-rental checks: {len(ev)}" + (f" · last: attestation_ok={last['attestation_ok']} reachable={last['reachable']} error={last['error']}" if last else ""))
        run([cli, "instances", "delete", iid], cli_env)
        print("  deleted; the host destroys the VM and the validator runs the readiness canary next (watch host-agent.log / validator.log)")
        time.sleep(20)
        print(f"  VMs now: {sh(['virsh', 'list', '--all', '--name']).replace(chr(10), ' ') or '-'}")
        return 0
    finally:
        for p in procs:
            p.send_signal(signal.SIGINT)
        for p in procs:
            try:
                p.wait(15)
            except subprocess.TimeoutExpired:
                p.kill()
        left = sh(["virsh", "list", "--all", "--name"]).split()
        if left:
            print(f"VMs still defined: {left} (the next host-agent start destroys them; or: virsh destroy <name>; virsh undefine --nvram <name>)")
        print(f"logs in {logs}")


if __name__ == "__main__":
    raise SystemExit(main())
