# Barrgo AI Stack - common commands. Run `make` or `make help` to list them.

ROUTER := services/lead-router
COMPOSE := docker compose
PY ?= python3
EXO_BASE_IMAGE ?= exoplatform/exo-community:7.2.0
LEADCAPTURE_REF ?= develop

.DEFAULT_GOAL := help
.PHONY: help init up up-full down logs seed demo test lint format check exo-addon backup clean

help: ## Show this list
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

init: ## Create .env with random secrets (first time only)
	@./scripts/init-env.sh

up: init ## Start the lead router, its database and the test inbox
	$(COMPOSE) up -d --build --wait lead-router lead-worker mailpit
	@echo ""
	@echo "  Intake form   http://localhost:8000"
	@echo "  Dashboard     http://localhost:8000/admin"
	@echo "  API docs      http://localhost:8000/docs"
	@echo "  Test inbox    http://localhost:8025"

up-full: init ## Start everything, including the eXo Platform workspace (~10 GB RAM)
	$(COMPOSE) --profile exo up -d --build
	@echo ""
	@echo "  eXo workspace http://exoapp.local  (first start takes several minutes;"
	@echo "                add '127.0.0.1 exoapp.local' to your hosts file)"

down: ## Stop everything (data is kept)
	$(COMPOSE) --profile exo down

logs: ## Follow the router and worker logs
	$(COMPOSE) logs -f lead-router lead-worker

seed: ## Load the six fictional demo attorneys
	$(COMPOSE) exec -T lead-router python -m app.seed

demo: seed ## Run the narrated end-to-end demo against the running stack
	@set -a; . ./.env; set +a; $(PY) scripts/demo.py

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

backup: ## Dump the router database to ./backups
	@mkdir -p backups
	$(COMPOSE) exec -T router-db pg_dump -U router router | gzip > backups/router-$$(date +%Y%m%d-%H%M%S).sql.gz
	@ls -1t backups | head -1

clean: ## Stop everything AND delete all data volumes (asks first)
	@read -p "This deletes all leads and eXo data. Type 'yes' to continue: " ok && [ "$$ok" = yes ]
	$(COMPOSE) --profile exo down -v
