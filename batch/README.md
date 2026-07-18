# Batch jobs (run on the Debian VM, in podman — see RULES.md)

Discovery sweeps for SatNOGS-decodable satellites. Typical run:

```bash
ssh confinia-ovh-debian 'TOKEN=$(grep -oP "(?<=^SATNOGS_TOKEN=).*" \
  ~/projects/overwatch/orbit-poc/.env); \
  podman run --rm -v ~/projects/overwatch/batch:/work -e TOKEN=$TOKEN \
  docker.io/library/python:3.12-slim bash -c \
  "pip install -q satnogs-decoders requests; python /work/probe3.py"'
```

Validated satellites get promoted into `orbit-poc/ingest/satellites.py`.
SatNOGS throttles hard: pace requests, honor Retry-After, prefer
norad_cat_id filters over search (search times out).
