# OW-over-YAMCS demo

A self-contained demo: a [YAMCS](https://yamcs.org) quickstart with its
simulator, feeding the [bridge](../), which pushes a simulated satellite's
telemetry into your Overwatch tenant. No hardware, no manual steps.

```sh
TENANT_KEY=<your Overwatch tenant key> docker compose up --build
```

The first build takes a few minutes (it builds YAMCS from the quickstart
and warms the Maven cache). Then:

- the `yamcs` service runs YAMCS **and** `simulator.py` together, so
  parameters have live values from the start (the plain quickstart emits
  nothing until the simulator is run);
- the `bridge` service subscribes to the parameters and pushes them into
  the tenant named by `TENANT_KEY`, under the satellite `QuickSat`.

Confirm it worked:

```sh
curl "$OVERWATCH_URL/v1/tenants/$TENANT_KEY/satellites"
# -> QuickSat with Battery1_Voltage, Battery2_Temp, ...
```

## Prove it

`prove.sh` is the scripted live proof ([#428](https://github.com/confinia/overwatch/issues/428)):
it brings the demo up, waits for QuickSat points to land in the tenant,
checks the bridge is on the WebSocket subscription (not the poll fallback),
restarts YAMCS and checks the bridge reconnects rather than downgrading,
checks no duplicate stamps reached the tenant, then runs the same bridge
with `YAMCS_MODE=poll` and switches back.

```sh
TENANT_KEY=<your tenant key> ./prove.sh
```

Exit 0 means every assertion held; a failure prints the step and the
bridge's log tail. Next to an Overwatch stack on the same host, add the
internal overlay (a host cannot reach its own public edge):

```sh
TENANT_KEY=... COMPOSE_FILES="-f docker-compose.yml -f docker-compose.internal.yml" \
PROVE_READ_URL=http://127.0.0.1:12000/api PROVE_READ_HOST=overwatch.confinia.io ./prove.sh
```

`PROVE_READ_URL` is where the script reads the tenant back (the edge on
localhost here), `PROVE_READ_HOST` the name that edge routes by.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `TENANT_KEY` | (required) | Your Overwatch tenant key |
| `OVERWATCH_URL` | `https://overwatch.confinia.io/api` | Where telemetry lands (point at your self-host instead if you run one) |

The parameters, instance (`myproject`), processor (`realtime`) and
satellite name are set in `docker-compose.yml`; edit them to taste.

## Notes

- **No host ports are published.** The bridge reaches YAMCS over the compose
  network as `http://yamcs:8090`. To open the YAMCS web UI locally, uncomment
  the loopback-bound `ports` line in the compose file.
- The quickstart's `simulator.py` plays its test data once (86,400 packets,
  24 h at 1 Hz) and then idles with the process alive; `start.sh` restarts
  it before the end so the demo never goes quiet.
- The YAMCS quickstart is pinned by commit in `Dockerfile.yamcs`, so the
  parameter names here stay valid. Override with
  `--build-arg QUICKSTART_REF=...`.
- This is the packaged form of the manual bring-up in the
  [integration guide](../../../../docs/yamcs-integration.md), and the basis
  for a public live demo ([#436](https://github.com/confinia/overwatch/issues/436)).
