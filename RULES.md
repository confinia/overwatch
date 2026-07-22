# Overwatch — Working rules

Conventions for humans and AI agents working on this project.

1. **No direct package-manager installs on hosts.** Avoid using tools like
   `pip` (or `npm`, `gem`, …) directly on the Mac or the VM. Run them inside
   a container instead (`podman run --rm docker.io/library/python:3.12-slim …`)
   or bake them into an image via a Dockerfile. Keeps hosts clean and
   experiments reproducible.
2. **Batch processing runs on the Debian VM by default** (`confinia-ovh-debian`
   / cka-ovh-dedicated-01), not on the Mac — inside podman containers per
   rule 1. The Mac is for editing and orchestration.
3. Services bind to `127.0.0.1` on the VM; the only public entrypoint is the
   shared caddy edge (see DEV.md).
4. Secrets live in `.env` files that are never committed and never rsynced
   (see `.gitignore` and the Makefile excludes).
5. Caddy routing: overwatch's own config lives in
   `orbit-poc/deploy/caddy/Caddyfile.tmpl` (reload via `make caddy`); the
   TLS stub at the platform edge via `make edge`. Never hand-edit on the VM.
6. **Git identity for this project**: commit as `contact@confinia.io`
   (set per-repo: `git config user.email contact@confinia.io`).
7. **No AI-authorship references.** Never credit an AI assistant as author or
   co-author — not in git commits (no `Co-Authored-By` trailers), not in web
   pages, not in source code, not in published artifacts. Authorship is
   `contact@confinia.io`.
8. **No AI-recognizable phrasing in public text.** Avoid words and tics that
   read as AI-written — "genuinely" is banned; same spirit for similar
   filler intensifiers ("truly", "deeply", "I'd love to"). Applies to posts,
   articles, pages, commit messages — anything public.
9. **All changes go through GitHub issues and pull requests** (since
   2026-07-21, repo public). Workflow: open an issue describing the change
   → branch (`feat/…`, `fix/…`, `ops/…`) → commits on the branch → PR
   referencing the issue (`Closes #N`) → merge → deploy via stage/promote.
   No more direct commits to `main` (hotfix exception: still via PR, just
   fast). Issues/PRs follow rules 7 and 8 — no AI credits, no AI-tell
   phrasing. Public issues double as a roadmap for the community.
10. **TENANT.md is the tenancy record.** Every time a conversation settles
   anything about organizations/tenants (identity, membership paths,
   isolation, billing linkage, API credentials), the outcome gets written
   into TENANT.md in the same session — the file must always reflect the
   current model, not an old one.
11. **English only.** All code comments and all Markdown docs are written in
   English — no exceptions, regardless of the language of the conversation
   that produced them. (Public-facing product copy may be localized; this
   rule is about code and repository documentation.)
