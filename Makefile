# Overwatch — deploy helpers. VM: confinia-ovh-debian (cka-ovh-dedicated-01).
VM      := confinia-ovh-debian
REMOTE  := ~/projects/overwatch
CONFINIA:= ~/projects/confinia

.PHONY: sync stage promote rollback status deploy deploy-full ingest caddy edge logs ps down

# Push the repo to the VM (secrets in .env stay VM-side, tarball stays local).
# version.env is generated from the VERSION file; the generated Caddyfile and
# LIVE_COLOR state are VM-side artifacts protected from --delete.
sync:
	printf 'OVERWATCH_VERSION=%s\n' "$$(tr -d '[:space:]' < VERSION)" > orbit-poc/version.env
	rsync -av --delete \
		--exclude 'orbit-poc.tar.gz' \
		--exclude 'orbit-poc/.env' \
		--exclude 'orbit-poc/deploy/geoip' \
		--exclude 'orbit-poc/deploy/caddy/Caddyfile' \
		--exclude 'orbit-poc/deploy/caddy/LIVE_COLOR' \
		--exclude '.DS_Store' \
		./ $(VM):$(REMOTE)/

# Blue/green deploys — two complete independent compose stacks (blue :808x,
# green :908x), switched by regenerating the app caddy config (graceful
# reload, zero downtime; the old color keeps running):
#   make stage     -> build the working tree into the CANDIDATE color;
#                     validate at https://staging.overwatch.confinia.io
#   make promote   -> candidate becomes LIVE (pure caddy color swap)
#   make rollback  -> instant: previous color still runs the previous version
#   make status    -> colors, health, container state
#   make deploy    -> stage + promote in one go (fast path, no manual gate)
# Core singletons via compose, created only if absent; `make ingest` or
# deploy-full to update them.
stage: sync
	ssh $(VM) 'set -e; cd $(REMOTE)/orbit-poc && test -f .env || cp .env.example .env; \
		podman-compose up -d --no-recreate db ingest grafana otel-collector prometheus caddy; \
		for c in $$(podman ps --format "{{.Names}}" | grep ^orbit-poc); do \
			podman update --restart=always $$c >/dev/null; done; \
		bash $(REMOTE)/deploy/slots.sh stage'

promote:
	ssh $(VM) 'bash $(REMOTE)/deploy/slots.sh promote'

rollback:
	ssh $(VM) 'bash $(REMOTE)/deploy/slots.sh rollback'

status:
	ssh $(VM) 'bash $(REMOTE)/deploy/slots.sh status'

deploy: stage promote

# Escape hatch: recreate the CORE via compose + rebuild both colors
# (brief downtime possible on the core; colors rebuild in place).
deploy-full: sync
	ssh $(VM) 'set -e; cd $(REMOTE)/orbit-poc && test -f .env || cp .env.example .env; \
		podman-compose up -d --build; \
		podman-compose -p blue -f docker-compose.blue.yml up -d --build; \
		podman-compose -p green -f docker-compose.green.yml up -d --build; \
		bash $(REMOTE)/deploy/slots.sh status'

# Reload overwatch's OWN caddy (orbit-poc stack) after editing
# orbit-poc/deploy/caddy/Caddyfile. Zero-downtime (graceful reload).
caddy: sync
	ssh $(VM) 'podman exec orbit-poc_caddy_1 caddy reload --config /etc/caddy/Caddyfile'

# Install/refresh the tiny TLS stub at the PLATFORM edge (rarely needed —
# the stub is stable by design). Uses the platform repo's own documented
# flow: copy into ../platform/sites/, rsync, deploy-edge.sh (ephemeral
# validation + graceful reload).
edge:
	cp deploy/caddy/overwatch.caddy ../platform/sites/
	rsync -az --delete --exclude '.git/' ../platform/ $(VM):projects/platform/
	ssh $(VM) 'cd ~/projects/platform && ./deploy-edge.sh'

# Rebuild + replace only ingest (background worker — no public downtime).
ingest: sync
	ssh $(VM) 'cd $(REMOTE)/orbit-poc && podman-compose build ingest && \
		podman rm -f orbit-poc_ingest_1 2>/dev/null; \
		cd $(REMOTE)/orbit-poc && podman-compose up -d --no-recreate ingest && \
		podman update --restart=always orbit-poc_ingest_1'

logs:
	ssh $(VM) 'cd $(REMOTE)/orbit-poc && podman-compose logs --tail=100'

ps:
	ssh $(VM) 'podman ps --format "{{.Names}}\t{{.Status}}\t{{.Ports}}"'

down:
	ssh $(VM) 'cd $(REMOTE)/orbit-poc && podman-compose down'
