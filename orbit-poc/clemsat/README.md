# CLEMSAT-1 — fake-satellite telemetry generator (#27)

Optional, standalone. Simulates a satellite in operation and pushes a
plausible telemetry stream (battery/temperatures/current/mode, driven by a
modelled orbit day/eclipse cycle) into a tenant via the PUBLIC API — no
backdoor, so it also exercises the real ingest path.

## Run a live demo tenant (on the VM)
    make clemsat-up      # provisions a 'clemsat-demo' tenant + starts the generator
    make clemsat-down    # stops it and removes the demo tenant's data

Point any org's service token at it to feed a real signed-in organization:
    podman run --rm -e TENANT_TOKEN=<token> -e API_BASE=https://overwatch.confinia.io \
      localhost/clemsat:latest

Env: API_BASE, TENANT_TOKEN (required), SATELLITE (default CLEMSAT-1), CADENCE_S (30).
