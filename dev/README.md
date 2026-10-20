# dev

Developer harness for the 8xB300 subnet repositories.

```
./bootstrap.sh                 # create /workspace/.venv and install every repo in editable mode (dependency order)
make test                      # run every repository's test-suite
python sim/run_sim.py --nodes 2 --duration 120
```

`sim/run_sim.py` launches the whole system as separate processes the way it runs in production, but in simulation mode:
a dev certificate authority stands in for NVIDIA's device PKI, each "server" is a `racksdock-host-agent --mode sim` whose VM is a
`racksdock-node-agent` process, the chain is a shared JSON file, and the validator's command runner emits real tool output formats.
It then walks the user journey with the `racksdock` CLI (sign-up, key, launch, attestation-verified `ssh --check-only`, delete) and
prints a status table until the duration elapses.

What simulation does not cover: libvirt/QEMU, VFIO passthrough, NVIDIA attestation against real RIMs, SSH into a real VM,
image builds. Those paths exist in the code (`LibvirtBackend`, `NvattestCliCollector`/`NvattestVerifier`, `SSHRunner`,
`racksdock-images build`) and are exercised on the reference node per docs/08 M0–M1.
