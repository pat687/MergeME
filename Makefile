# Barrgo AI Stack - common commands. Run `make` or `make help` to list them.

ROUTER := services/lead-router
COMPOSE := docker compose
PY ?= python3
# Add-on build defaults: lead-capture 1.0.0-M42 is built against eXo 7.0 (see infra/exo/README.md).
EXO_BASE_IMAGE ?= exoplatform/exo-community:7.0.0
LEADCAPTURE_REF ?= 1.0.0-M42

# Ports for the printed URLs: a value set in the shell wins, then .env, then the default.
# Only these are read: including .env wholesale would let it override the shell and hand
# make's unquoted copies of every value to docker compose.
env_value = $(shell sed -n 's/^$(1)=//p' .env 2>/dev/null | tail -n 1)
ROUTER_HTTP_PORT ?= $(or $(call env_value,ROUTER_HTTP_PORT),8000)
MAILPIT_UI_PORT ?= $(or $(call env_value,MAILPIT_UI_PORT),8025)
EXO_HTTP_PORT ?= $(or $(call env_value,EXO_HTTP_PORT),80)

.DEFAULT_GOAL := help
.PHONY: help init up up-full down logs seed demo ui-check test lint format check exo-addon backup clean

help: ## Show this list
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

init: ## Create .env with random secrets (first time only)
	@./scripts/init-env.sh

up: init ## Start the lead router, its database and the test inbox
	$(COMPOSE) up -d --build --wait lead-router lead-worker mailpit
	@echo ""
	@echo "  Intake form   http://localhost:$(ROUTER_HTTP_PORT)"
	@echo "  Dashboard     http://localhost:$(ROUTER_HTTP_PORT)/admin"
	@echo "  API docs      http://localhost:$(ROUTER_HTTP_PORT)/docs"
	@echo "  Test inbox    http://localhost:$(MAILPIT_UI_PORT)"

up-full: init ## Start everything, including the eXo Platform workspace (~10 GB RAM)
	$(COMPOSE) --profile exo up -d --build
	@echo ""
	@echo "  eXo workspace http://exoapp.local:$(EXO_HTTP_PORT)  (first start takes several minutes;"
	@echo "                add '127.0.0.1 exoapp.local' to your hosts file)"

down: ## Stop everything (data is kept)
	$(COMPOSE) --profile exo down

logs: ## Follow the router and worker logs
	$(COMPOSE) logs -f lead-router lead-worker

seed: ## Load the six fictional demo attorneys
	$(COMPOSE) exec -T lead-router python -m app.seed

demo: seed ## Run the narrated end-to-end demo against the running stack
	@set -a; . ./.env; set +a; $(PY) scripts/demo.py

ui-check: ## Browser regression checks of the three pages (needs Node; installs Playwright)
	@npm install --no-save --silent --prefix .cache/pw playwright@1.56.1
	@.cache/pw/node_modules/.bin/playwright install chromium
	@set -a; . ./.env; set +a; NODE_PATH=.cache/pw/node_modules node scripts/ui_smoke.js

test: ## Run the automated tests
	cd $(ROUTER) && $(PY) -m pytest

lint: ## Check code style
	cd $(ROUTER) && ruff check . && ruff format --check .

format: ## Fix code style
	cd $(ROUTER) && ruff format . && ruff check --fix .

check: lint test ## Everything CI runs
	$(COMPOSE) config --quiet

exo-addon: ## Build eXo + lead-capture add-on (see infra/exo/README.md for versions)
	docker build --build-arg EXO_IMAGE=$(EXO_BASE_IMAGE) --build-arg LEADCAPTURE_REF=$(LEADCAPTURE_REF) \
		-t barrgo/exo-leadcapture:local infra/exo

backup: ## Dump the router database to ./backups (fails loudly if the dump fails)
	@mkdir -p backups
	@f=backups/router-$$(date +%Y%m%d-%H%M%S).sql; \
	if $(COMPOSE) exec -T router-db pg_dump -U router --clean --if-exists router > $$f.tmp; then \
		mv $$f.tmp $$f && gzip $$f && echo "Backup written: $$f.gz"; \
	else \
		rm -f $$f.tmp; echo "Backup FAILED: pg_dump did not complete" >&2; exit 1; \
	fi

clean: ## Stop everything AND delete all data volumes (asks first)
	@read -p "This deletes all leads and eXo data. Type 'yes' to continue: " ok && [ "$$ok" = yes ]
	$(COMPOSE) --profile exo down -v
