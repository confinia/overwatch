# Overwatch — deploy helpers. VM: confinia-ovh-debian (cka-ovh-dedicated-01).
VM      := confinia-ovh-debian
REMOTE  := ~/projects/overwatch
CONFINIA:= ~/projects/confinia

.PHONY: sync stage promote deploy deploy-full ingest caddy logs ps down

# Push the repo to the VM (secrets in .env stay VM-side, tarball stays local).
sync:
	rsync -av --delete \
		--exclude 'orbit-poc.tar.gz' \
		--exclude 'orbit-poc/.env' \
		--exclude 'orbit-poc/deploy/geoip' \
		--exclude '.DS_Store' \
		./ $(VM):$(REMOTE)/

# Staged zero-downtime deploys (deploy/slots.sh):
#   make stage    -> build candidate into the staging slot; validate it at
#                    https://staging.overwatch.confinia.io (basic-auth)
#   make promote  -> flip validated candidate to production (no downtime:
#                    traffic covers via staging during the prod restart)
#   make deploy   -> stage + promote in one go (fast path, no manual gate)
# Singletons via compose, created only if absent; use `make ingest` or
# deploy-full to update them.
stage: sync
	ssh $(VM) 'set -e; cd $(REMOTE)/orbit-poc && test -f .env || cp .env.example .env; \
		podman-compose up -d --no-recreate db ingest grafana otel-collector prometheus; \
		for c in $$(podman ps --format "{{.Names}}" | grep ^orbit-poc); do \
			podman update --restart=always $$c >/dev/null; done; \
		bash $(REMOTE)/deploy/slots.sh stage'

promote:
	ssh $(VM) 'bash $(REMOTE)/deploy/slots.sh promote'

deploy: stage promote

# Escape hatch: recreate EVERYTHING via compose (brief downtime — removes the
# prod/staging slots first so compose can rebind 8081/8082).
deploy-full: sync
	ssh $(VM) 'set -e; cd $(REMOTE)/orbit-poc && test -f .env || cp .env.example .env; \
		podman rm -f overwatch_web_prod overwatch_web_staging \
			overwatch_api_prod overwatch_api_staging 2>/dev/null || true; \
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
