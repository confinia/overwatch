# Overwatch — deploy helpers. VM: confinia-ovh-debian (cka-ovh-dedicated-01).
VM      := confinia-ovh-debian
REMOTE  := ~/projects/overwatch
CONFINIA:= ~/projects/confinia

.PHONY: sync deploy caddy logs ps down

# Push the repo to the VM (secrets in .env stay VM-side, tarball stays local).
sync:
	rsync -av --delete \
		--exclude 'orbit-poc.tar.gz' \
		--exclude 'orbit-poc/.env' \
		--exclude 'orbit-poc/deploy/geoip' \
		--exclude '.DS_Store' \
		./ $(VM):$(REMOTE)/

# Zero-downtime deploy: singletons via compose (created only if absent —
# they are stateful or have no public HTTP surface; use deploy-full or
# `make ingest` to update them), then web+api into the idle blue/green slot
# (deploy/bluegreen.sh; caddy health-checks flip traffic, no dropped requests).
deploy: sync
	ssh $(VM) 'set -e; cd $(REMOTE)/orbit-poc && test -f .env || cp .env.example .env; \
		podman-compose up -d --no-recreate db ingest grafana otel-collector prometheus; \
		for c in $$(podman ps --format "{{.Names}}" | grep ^orbit-poc); do \
			podman update --restart=always $$c >/dev/null; done; \
		bash $(REMOTE)/deploy/bluegreen.sh'

# Escape hatch: recreate EVERYTHING via compose (brief downtime — removes the
# blue/green slots first so compose can rebind 8081/8082).
deploy-full: sync
	ssh $(VM) 'set -e; cd $(REMOTE)/orbit-poc && test -f .env || cp .env.example .env; \
		podman rm -f overwatch_web_blue overwatch_web_green \
			overwatch_api_blue overwatch_api_green 2>/dev/null || true; \
		podman-compose up -d --build'

# Install/refresh the vhost in the shared caddy edge and reload it.
# Also copied into the LOCAL confinia repo (deploy/sites/ is confinia-managed:
# a confinia-side sync would otherwise delete our vhost from the VM).
caddy:
	mkdir -p ../confinia/deploy/sites
	cp deploy/caddy/overwatch.caddy ../confinia/deploy/sites/
	ssh $(VM) 'mkdir -p $(CONFINIA)/deploy/sites'
	scp deploy/caddy/overwatch.caddy $(VM):$(CONFINIA)/deploy/sites/
	ssh $(VM) 'podman exec confinia_caddy_1 caddy reload --config /etc/caddy/Caddyfile'

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
