.PHONY: help up down reset provision place-order

help:  ## Show this help
	@grep -E '^[a-zA-Z][a-zA-Z0-9_-]*:.*##' $(MAKEFILE_LIST) | \
		sort | awk -F ':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

up:  ## Bring up the demo
	docker compose up -d

down:  ## Stop services (volumes preserved)
	docker compose down

reset:  ## Wipe volumes and bring up fresh
	docker compose down -v
	docker compose up -d

provision:  ## Configure all 3 neurons via EMQX Neuron REST API
	uv run tools/provision_neuron.py

place-order:  ## Place one program-a order (dev helper)
	uv run tools/place_order.py --recipe program-a --qty 1
