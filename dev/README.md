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

## Bare-metal VM test (`dev/vmtest/`)

`vmtest/run_vm_test.py` is the single-box version of the production path: marketplace, host-agent in **real** mode (libvirt/KVM,
VFIO passthrough, NAT port forwarding), miner bootstrap, validator with the SSH runner, tenant journey. Prepare the host with
`racksdock-node/host-image/scripts/setup-host.sh`, build the images with `vmtest/build-images.sh verify ubuntu24.04`, run as root.
Profile and trust mode come from `/etc/racksdock/host-agent.env` (`PROFILE=b300x8 TRUST_MODE=prod` on a B300 reference node;
`rtx4090x8-dev` + dev CA on the 8x4090 lab box, see `racksdock-node/docs/rtx4090-vm-test.md`).
