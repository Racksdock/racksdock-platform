from __future__ import annotations

import base64
import json
import os
import shutil
from pathlib import Path

import typer
from racksdock_subnet.core.keys import SigningKey
from racksdock_subnet.core.nonce import new_nonce_hex
from racksdock_subnet.core.schemas import CreateInstanceRequest, DockerConfiguration, EnvVar, LaunchConfiguration, LaunchType, PortMapping, ScriptConfiguration
from racksdock_subnet.verifier.config import TrustConfig

from . import config
from .client import call, make_client
from .verify import verify_instance, verify_qualification


def resolve_trust(c) -> TrustConfig:
    """RACKSDOCK_TRUST_MODE wins; otherwise ask the platform. In dev mode fetch its public simulated CA into ~/.racksdock/devca."""
    import os

    from racksdock_subnet.verifier.config import TrustConfig

    if os.environ.get("RACKSDOCK_TRUST_MODE"):
        return TrustConfig.from_env()
    mode = call(c, "GET", "/v1/instances/types")["instance_types"][0].get("trust_mode")
    if mode is None:  # older platform build: the dev-CA endpoint exists only in dev trust mode
        mode = "dev" if c.get("/v1/dev/devca").status_code == 200 else "prod"
    if mode != "dev":
        return TrustConfig(mode="prod", dev_ca_dir=None)
    ca_dir = config.HOME / "devca"
    if not (ca_dir / "root.pem").exists():
        from racksdock_subnet.verifier.cli import fetch_devca

        fetch_devca(str(c.base_url), ca_dir)
        typer.echo(f"note: platform runs in dev trust mode (simulated attestation); fetched its public dev CA into {ca_dir}", err=True)
    return TrustConfig(mode="dev", dev_ca_dir=ca_dir)

app = typer.Typer(help="racksdock: rent dedicated 8xB300 servers", no_args_is_help=True)
auth_app = typer.Typer(help="authentication")
nodes_app = typer.Typer(help="browse and verify nodes")
inst_app = typer.Typer(help="instances")
keys_app = typer.Typer(help="SSH keys")
tpl_app = typer.Typer(help="templates")
for name, sub in (("auth", auth_app), ("nodes", nodes_app), ("instances", inst_app), ("ssh-keys", keys_app), ("templates", tpl_app)):
    app.add_typer(sub, name=name)


def _print(obj) -> None:
    typer.echo(json.dumps(obj, indent=1))


# ---- auth ---------------------------------------------------------------------
@auth_app.command("login")
def auth_login(api_key: str | None = typer.Option(None), email: str | None = None, password: str | None = None, api_url: str | None = None):
    cfg = config.load()
    if api_url:
        cfg["api_url"] = api_url
        config.save(cfg)  # make_client reads the saved config: the new URL must be on disk before the login call
    if api_key:
        cfg["api_key"] = api_key
    elif email and password:
        with make_client(auth=False) as c:
            cfg["api_key"] = call(c, "POST", "/v1/account/login", json={"email": email, "password": password})["api_key"]
    else:
        raise typer.BadParameter("pass --api-key or --email/--password")
    config.save(cfg)
    typer.echo(f"logged in to {cfg['api_url']}")


@auth_app.command("signup")
def auth_signup(email: str, password: str, api_url: str | None = None):
    cfg = config.load()
    if api_url:
        cfg["api_url"] = api_url
        config.save(cfg)
    with make_client(auth=False) as c:
        data = call(c, "POST", "/v1/account/signup", json={"email": email, "password": password})
    cfg["api_key"] = data["api_key"]
    config.save(cfg)
    typer.echo(f"account {data['user_id']} created; logged in")


@app.command()
def account():
    with make_client() as c:
        _print(call(c, "GET", "/v1/account"))


# ---- nodes --------------------------------------------------------------------
@nodes_app.command("list")
def nodes_list(region: str | None = None, available: bool = False):
    with make_client(auth=False) as c:
        types = call(c, "GET", "/v1/instances/types")["instance_types"][0]
    rows = [a for a in types["availability"] if (not region or a["region"] == region) and (not available or a["available"])]
    typer.echo(f"{'provider':<16} {'region':<12} {'$/h':>7} {'perf':>5} {'avail':>5} {'badges':<60} node_id")
    for a in rows:
        typer.echo(f"{a['provider'][:16]:<16} {a['region'][:12]:<12} {a['hourly_price_cents'] / 100:>7.2f} {a['perf']:>5.2f} {'yes' if a['available'] else 'no':>5} {','.join(a['badges']):<60} {a['node_id']}")
    if not rows:
        typer.echo("(no nodes) — a node appears here once a validator has qualified it; `instances create --node auto` takes any available node")


@nodes_app.command("verify")
def nodes_verify(node_id: str):
    with make_client(auth=False) as c:
        ver = call(c, "GET", f"/v1/nodes/{node_id}/verification")
        hist = [h for h in ver["qualification_history"] if h["kind"] in ("full", "weekly")]
        if not hist:
            typer.echo("no qualification records")
            raise typer.Exit(1)
        env = call(c, "GET", f"/v1/evidence/{hist[0]['hash']}")["envelope"]
    v = verify_qualification(env, ver["registration"]["gpu_ids"])
    for name, ok, detail in v.checks:
        typer.echo(f"[{'ok' if ok else 'FAIL'}] {name} {detail}")
    typer.echo(f"node {node_id[:24]}: {'VERIFIED' if v.ok else 'NOT VERIFIED'} (perf {ver['perf']}, status {ver['status']})")
    raise typer.Exit(0 if v.ok else 1)


# ---- instances ----------------------------------------------------------------
def _parse_env(items: list[str]) -> list[EnvVar]:
    out = []
    for it in items:
        if "=" not in it:
            raise typer.BadParameter(f"env must be NAME=value: {it}")
        k, v = it.split("=", 1)
        out.append(EnvVar(name=k, value=v))
    return out


@inst_app.command("create")
def instances_create(
    name: str = typer.Option("my-racksdock"), node: str = typer.Option("auto"), region: str | None = None, os_: str | None = typer.Option(None, "--os"),
    ssh_key: list[str] = typer.Option([], "--ssh-key"), script: Path | None = None, docker: str | None = None, args: str = "", port: list[str] = typer.Option([], "--port"),
    env: list[str] = typer.Option([], "--env"), tag: list[str] = typer.Option([], "--tag"), metrics: bool = False, body: Path | None = None, schema: bool = False,
    alert_spend: str | None = None, auto_delete_spend: str | None = None, auto_delete_at: str | None = None,
):
    if schema:
        _print(CreateInstanceRequest.model_json_schema())
        return
    if body:
        req = CreateInstanceRequest.model_validate_json(body.read_text())
    else:
        lc = None
        if script and docker:
            raise typer.BadParameter("use either --script or --docker")
        if script:
            lc = LaunchConfiguration(type=LaunchType.script, script_configuration=ScriptConfiguration(base64_script=base64.b64encode(script.read_bytes()).decode()))
        if docker:
            pms = []
            for p in port:
                cport, _, hport = p.partition(":")
                pms.append(PortMapping(container_port=int(cport), host_port=int(hport or cport)))
            lc = LaunchConfiguration(type=LaunchType.docker, docker_configuration=DockerConfiguration(image=docker, args=args, port_mappings=pms))
        req = CreateInstanceRequest(name=name, node_id=node, region=region, os=os_, ssh_key_ids=list(ssh_key), launch_configuration=lc, envs=_parse_env(env), tags=list(tag),
                                    enable_metrics=metrics, alert={"spend_threshold": alert_spend} if alert_spend else None,
                                    auto_delete={"spend_threshold": auto_delete_spend, "date_threshold": auto_delete_at} if (auto_delete_spend or auto_delete_at) else None)
    req.tenant_nonce = req.tenant_nonce or new_nonce_hex()
    with make_client() as c:
        if not req.ssh_key_ids:
            keys = call(c, "GET", "/v1/sshkeys")["ssh_keys"]
            req.ssh_key_ids = [k["id"] for k in keys if k["is_default"]] or ([keys[0]["id"]] if keys else [])
        keys = {k["id"]: k["public_key"] for k in call(c, "GET", "/v1/sshkeys")["ssh_keys"]}
        data = call(c, "POST", "/v1/instances/create", json=req.model_dump(mode="json"))
    # keep what we need to verify independently later
    local = {"tenant_nonce": req.tenant_nonce, "ssh_public_keys": [keys[k] for k in req.ssh_key_ids if k in keys],
             "user_data_input": {"ssh_public_keys": [keys[k] for k in req.ssh_key_ids if k in keys], "launch_configuration": req.launch_configuration.model_dump(mode="json") if req.launch_configuration else None,
                                 "envs": [e.model_dump() for e in req.envs], "networking": req.networking.model_dump() if req.networking else None, "enable_metrics": req.enable_metrics}}
    config.instance_record_path(data["id"]).write_text(json.dumps(local, indent=1))
    typer.echo(data["id"])


@inst_app.command("list")
def instances_list(all_: bool = typer.Option(False, "--all")):
    with make_client() as c:
        data = call(c, "GET", "/v1/instances", params={"include_deleted": all_})
    typer.echo(f"{'id':<24} {'name':<16} {'status':<16} {'ip':<16} {'os':<22} {'cost':>8}")
    for i in data["instances"]:
        typer.echo(f"{i['id']:<24} {i['name'][:16]:<16} {i['status']:<16} {i['ip'] or '-'!s:<16} {i['configuration']['os'][:22]:<22} {i['cost_estimate']:>8}")


@inst_app.command("get")
def instances_get(instance_id: str):
    with make_client() as c:
        _print(call(c, "GET", f"/v1/instances/{instance_id}/info"))


@inst_app.command("restart")
def instances_restart(instance_id: str, hard: bool = False):
    with make_client() as c:
        _print(call(c, "POST", f"/v1/instances/{instance_id}/restart", params={"hard": hard}))


@inst_app.command("delete")
def instances_delete(instance_id: str):
    with make_client() as c:
        _print(call(c, "POST", f"/v1/instances/{instance_id}/delete"))


# ---- ssh ----------------------------------------------------------------------
@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def ssh(ctx: typer.Context, instance_id: str, check_only: bool = False, insecure: bool = False,
        identity: Path | None = typer.Option(None, "--identity", "-i", help="private key; default: the key racksdock generated or added for this instance")):
    """Verify attestation and the host key, pin it, then connect."""
    with make_client() as c:
        info = call(c, "GET", f"/v1/instances/{instance_id}/info")
        if info["status"] not in ("active", "degraded"):
            typer.echo(f"instance is {info['status']}: {info.get('status_details') or ''}", err=True)
            raise typer.Exit(2)
        recs = call(c, "GET", "/v1/evidence", params={"instance_id": instance_id, "kind": "boot_evidence"})["records"]
        reg = call(c, "GET", f"/v1/nodes/{info['node_id']}/verification")["registration"]
        trust = resolve_trust(c)
    if not recs:
        typer.echo("no boot evidence published for this instance", err=True)
        if not insecure:
            raise typer.Exit(1)
    p = config.instance_record_path(instance_id)
    local = json.loads(p.read_text()) if p.exists() else None
    verdict = verify_instance(info, recs[-1]["envelope"], reg["gpu_ids"], local, trust=trust) if recs else None
    if verdict:
        for name, ok, detail in verdict.checks:
            typer.echo(f"[{'ok' if ok else 'FAIL'}] {name} {detail}", err=True)
        typer.echo(f"attestation: {'VERIFIED' if verdict.ok else 'FAILED'} · host key {verdict.fingerprint}", err=True)
        if not verdict.ok and not insecure:
            typer.echo("refusing to connect (use --insecure to override)", err=True)
            raise typer.Exit(1)
    if check_only:
        raise typer.Exit(0 if (verdict and verdict.ok) else 1)
    known = config.HOME / "known_hosts"
    config.HOME.mkdir(parents=True, exist_ok=True)
    host_key_line = (verdict.host_key_line if verdict else info.get("host_key_line")) or ""
    hostpat = f"[{info['ip']}]:{info['ssh_port']}" if info["ssh_port"] != 22 else info["ip"]
    lines = [line for line in (known.read_text().splitlines() if known.exists() else []) if not line.startswith(hostpat + " ")]
    lines.append(f"{hostpat} {' '.join(host_key_line.split()[:2])}")
    known.write_text("\n".join(lines) + "\n")
    key = str(identity) if identity else config.private_key_for((local or {}).get("ssh_public_keys", []))
    cmd = [shutil.which("ssh") or "ssh", "-o", f"UserKnownHostsFile={known}", "-o", "StrictHostKeyChecking=yes", "-p", str(info["ssh_port"])]
    if key:
        cmd += ["-i", key]
    else:
        typer.echo("note: no matching private key known to racksdock; relying on your ssh agent/default keys (use -i to specify one)", err=True)
    cmd += [f"{info['ssh_user']}@{info['ip']}", *ctx.args]
    typer.echo("+ " + " ".join(cmd), err=True)
    os.execvp(cmd[0], cmd)


# ---- ssh keys -----------------------------------------------------------------
@keys_app.command("add")
def keys_add(name: str = typer.Option(...), file: Path = typer.Option(..., help="public key file (.pub)")):
    pub = file.read_text().strip()
    with make_client() as c:
        _print(call(c, "POST", "/v1/sshkeys/add", json={"name": name, "public_key": pub}))
    priv = file.with_suffix("") if file.suffix == ".pub" else None
    if priv and priv.exists():
        config.remember_private_key(pub, str(priv))


@keys_app.command("generate")
def keys_generate(name: str = typer.Option("racksdock-key"), out: Path | None = None):
    """Generate an ed25519 keypair locally; only the public key is uploaded."""
    out = out or (Path.home() / ".ssh" / f"racksdock_{name}")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        raise typer.BadParameter(f"{out} exists")
    key = SigningKey.generate()
    out.write_bytes(key.to_openssh_private())
    out.chmod(0o600)
    pub = key.public.ssh_public_line(name)
    out.with_suffix(".pub").write_text(pub + "\n")
    with make_client() as c:
        data = call(c, "POST", "/v1/sshkeys/add", json={"name": name, "public_key": pub})
    config.remember_private_key(pub, str(out))
    typer.echo(f"private key: {out}\npublic key uploaded: {data['id']}")


@keys_app.command("list")
def keys_list():
    with make_client() as c:
        _print(call(c, "GET", "/v1/sshkeys"))


@keys_app.command("set-default")
def keys_default(key_id: str):
    with make_client() as c:
        _print(call(c, "POST", f"/v1/sshkeys/{key_id}/setdefault"))


@keys_app.command("delete")
def keys_delete(key_id: str):
    with make_client() as c:
        _print(call(c, "POST", f"/v1/sshkeys/{key_id}/delete"))


# ---- templates ----------------------------------------------------------------
@tpl_app.command("save")
def tpl_save(name: str, body: Path):
    with make_client() as c:
        _print(call(c, "POST", "/v1/templates/save", json={"name": name, "body": json.loads(body.read_text())}))


@tpl_app.command("list")
def tpl_list():
    with make_client() as c:
        _print(call(c, "GET", "/v1/templates"))


@tpl_app.command("get")
def tpl_get(template_id: str):
    with make_client() as c:
        _print(call(c, "GET", f"/v1/templates/{template_id}/info"))


@tpl_app.command("update")
def tpl_update(template_id: str, name: str, body: Path):
    with make_client() as c:
        _print(call(c, "POST", f"/v1/templates/{template_id}/update", json={"name": name, "body": json.loads(body.read_text())}))


@tpl_app.command("delete")
def tpl_delete(template_id: str):
    with make_client() as c:
        _print(call(c, "POST", f"/v1/templates/{template_id}/delete"))


if __name__ == "__main__":
    app()
