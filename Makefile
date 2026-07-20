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

# Build + (re)start the stack on the VM.
deploy: sync
	ssh $(VM) 'cd $(REMOTE)/orbit-poc && test -f .env || cp .env.example .env; \
		cd $(REMOTE)/orbit-poc && podman-compose up -d --build'

# Install/refresh the vhost in the shared caddy edge and reload it.
# Also copied into the LOCAL confinia repo (deploy/sites/ is confinia-managed:
# a confinia-side sync would otherwise delete our vhost from the VM).
caddy:
	mkdir -p ../confinia/deploy/sites
	cp deploy/caddy/overwatch.caddy ../confinia/deploy/sites/
	ssh $(VM) 'mkdir -p $(CONFINIA)/deploy/sites'
	scp deploy/caddy/overwatch.caddy $(VM):$(CONFINIA)/deploy/sites/
	ssh $(VM) 'podman exec confinia_caddy_1 caddy reload --config /etc/caddy/Caddyfile'

logs:
	ssh $(VM) 'cd $(REMOTE)/orbit-poc && podman-compose logs --tail=100'

ps:
	ssh $(VM) 'podman ps --format "{{.Names}}\t{{.Status}}\t{{.Ports}}"'

down:
	ssh $(VM) 'cd $(REMOTE)/orbit-poc && podman-compose down'
