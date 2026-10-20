#!/bin/bash
# A100 WORKFLOW TEST ONLY — prepares one Ubuntu machine with a single NVIDIA GPU as a racksdock node in "static" mode.
# Run as root on the A100 box. Installs tools the qualification suite calls, creates the `ubuntu` SSH user, installs the
# racksdock packages into /opt/racksdock/venv, and writes /etc/racksdock/a100.env for run_a100_test.py.
set -euo pipefail
REPOS="${REPOS:-$(cd "$(dirname "$0")/../../.." && pwd)}"   # directory holding racksdock-subnet, racksdock-node, racksdock-platform
echo "1. packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-venv fio iperf3 sysbench memtester pciutils openssh-server curl jq
if ! command -v dcgmi >/dev/null; then
  . /etc/os-release; DIST="ubuntu$(echo "$VERSION_ID" | tr -d .)"
  curl -fsSL "https://developer.download.nvidia.com/compute/cuda/repos/${DIST}/x86_64/cuda-keyring_1.1-1_all.deb" -o /tmp/cuda-keyring.deb && dpkg -i /tmp/cuda-keyring.deb && apt-get update -q
  apt-get install -y -q datacenter-gpu-manager || echo "WARNING: DCGM not installed; Q4 will fail (install datacenter-gpu-manager manually)"
fi
command -v nv-hostengine >/dev/null && (nv-hostengine >/dev/null 2>&1 || true)
echo "2. ubuntu user (the tenant/validator SSH user)"
id ubuntu >/dev/null 2>&1 || useradd -m -s /bin/bash ubuntu
usermod -p '*' ubuntu    # no password, but not 'locked': sshd with UsePAM no refuses locked accounts even for key login
echo "ubuntu ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ubuntu && chmod 440 /etc/sudoers.d/ubuntu
install -d -o ubuntu -g ubuntu -m 700 /home/ubuntu/.ssh
touch /home/ubuntu/.ssh/authorized_keys && chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys && chmod 600 /home/ubuntu/.ssh/authorized_keys
systemctl enable --now ssh 2>/dev/null || service ssh start || true
echo "3. in-image tools"
install -m 755 "$REPOS/racksdock-node/image-build/overlay/usr/local/bin/"racksdock-{memtest,ramtest,marker,stream,launch} /usr/local/bin/
mkdir -p /etc/racksdock /tmp/racksdock-fio /run/racksdock
echo "4. racksdock packages"
PY="${PYTHON:-python3}"
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "ERROR: $PY is older than 3.11. On Ubuntu 22.04: add-apt-repository -y ppa:deadsnakes/ppa && apt-get install -y python3.11 python3.11-venv, then rerun with PYTHON=python3.11"; exit 1
fi
"$PY" -m venv /opt/racksdock/venv
/opt/racksdock/venv/bin/pip install -q --upgrade pip
/opt/racksdock/venv/bin/pip install -q -e "$REPOS/racksdock-subnet[validator]" -e "$REPOS/racksdock-node[nvml]" -e "$REPOS/racksdock-platform"
echo "5. simulated GPU identity (Ampere has no device attestation; the platform plays NVIDIA's role in dev mode)"
MEM_MIB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1 | tr -d ' ')
PCI=$(nvidia-smi --query-gpu=pci.device_id --format=csv,noheader | head -1 | tr -d ' ' | sed -E 's/^0x([0-9A-Fa-f]{4})([0-9A-Fa-f]{4})$/\2:\1/' | tr 'A-F' 'a-f')
if [ -n "${PLATFORM_URL:-}" ]; then
  RACKSDOCK_PROFILE=a100-dev /opt/racksdock/venv/bin/racksdock-verify devca fetch "$PLATFORM_URL" /etc/racksdock/devca
  RACKSDOCK_PROFILE=a100-dev /opt/racksdock/venv/bin/racksdock-verify devca fetch-node "$PLATFORM_URL" /etc/racksdock/node --count 1 --pci-id "$PCI" --memory-mib "$MEM_MIB"
else
  /opt/racksdock/venv/bin/racksdock-verify devca init /etc/racksdock/devca 2>/dev/null || true   # single-box test: the box also runs the platform
  RACKSDOCK_PROFILE=a100-dev /opt/racksdock/venv/bin/racksdock-verify devca issue-node /etc/racksdock/devca /etc/racksdock/node --count 1 --pci-id "$PCI" --memory-mib "$MEM_MIB"
fi
PUBIP="${PUBLIC_IP:-127.0.0.1}"
cat > /etc/racksdock/a100.env <<ENV
RACKSDOCK_PROFILE=a100-dev
RACKSDOCK_TRUST_MODE=dev
RACKSDOCK_DEV_CA_DIR=/etc/racksdock/devca
RACKSDOCK_SIM_NODE_DIR=/etc/racksdock/node
RACKSDOCK_STATIC_PUBLIC_IP=$PUBIP
RACKSDOCK_STATIC_SSH_PORT=22
RACKSDOCK_STATIC_AUTHORIZED_KEYS=/home/ubuntu/.ssh/authorized_keys
RACKSDOCK_SSH_USER=ubuntu
ENV
echo "done. Next: /opt/racksdock/venv/bin/python $REPOS/racksdock-platform/dev/a100/run_a100_test.py"
