# cli (`racksdock_platform.cli`)

```
racksdock auth login --api-key racksdock_...           # or: racksdock auth signup / login --email --password
racksdock nodes list [--region R] [--available]
racksdock nodes verify <node_id>                  # re-verify the latest qualification evidence signatures locally
racksdock instances create --name train-1 --node auto --os ubuntu24.04_cuda13.x [--ssh-key ID]... [--script FILE | --docker IMAGE --args "..." --port 8000:8000] [--env K=V]... [--body FILE | --schema]
racksdock instances list | get <id> | restart <id> | delete <id>
racksdock ssh <id> [--check-only] [--insecure] [-- ssh args]
racksdock ssh-keys add --name N --file PUB | generate --name N | list | set-default <id> | delete <id>
racksdock templates save|list|get|update|delete
```

`racksdock ssh` fails closed (docs/05 §5): it fetches the instance's boot evidence, recomputes `SHA256(tenant_nonce || host_key || SHA256(user_data))` from the tenant nonce it generated at create time and the user-data it submitted, verifies the NVIDIA evidence for all eight GPUs against the node's public registration, checks the presented host key is the attested one, pins it in `~/.racksdock/known_hosts`, and only then execs `ssh`. `--insecure` connects anyway and prints why verification failed.

Config lives in `~/.racksdock/config.json` (`RACKSDOCK_API_URL`, `RACKSDOCK_API_KEY` override). Trust mode for evidence verification follows `RACKSDOCK_TRUST_MODE` (`prod` uses nvattest locally; `dev` uses the simulator's dev CA).
