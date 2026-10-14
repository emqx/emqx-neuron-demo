.PHONY: help up down reset provision place-order booth-reset booth-up check

help:  ## Show this help
	@grep -E '^[a-zA-Z][a-zA-Z0-9_-]*:.*##' $(MAKEFILE_LIST) | \
		sort | awk -F ':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

up:  ## Bring up the demo
	docker compose up -d --build

down:  ## Stop services (volumes preserved)
	docker compose down

reset:  ## Wipe volumes and bring up fresh
	docker compose down -v
	docker compose up -d

provision:  ## Configure all 3 neurons via EMQX Neuron REST API
	uv run tools/provision_neuron.py

place-order:  ## Place one program-a order (dev helper)
	uv run tools/place_order.py --recipe program-a --qty 1

booth-reset:  ## Reset only the walkthrough; preserve volumes and production history
	curl --fail --silent --show-error -X POST http://localhost:8080/api/demo/reset -H 'Content-Type: application/json' -d '{}'

booth-up:  ## Start the authenticated HTTPS profile using .env.booth
	docker compose --env-file .env.booth -f docker-compose.yml -f docker-compose.booth.yml up -d --build

check:  ## Check controller, reset, freshness and hosted-auth behavior
	uv run --with flask==3.1.3 --with paho-mqtt==2.1.0 --with waitress==3.0.2 python -m unittest discover -s tests -v
