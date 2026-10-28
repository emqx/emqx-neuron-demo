.PHONY: help security-init up down reset provision place-order booth-reset booth-loop booth-up check check-ui check-restrictions

help:  ## Show this help
	@grep -E '^[a-zA-Z][a-zA-Z0-9_-]*:.*##' $(MAKEFILE_LIST) | \
		sort | awk -F ':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

security-init:  ## Generate local credentials (preserves existing secrets)
	python3 tools/init_security.py

up: security-init  ## Bring up the demo
	docker compose up -d --build

down:  ## Stop services (volumes preserved)
	docker compose down

reset:  ## Wipe volumes and bring up fresh
	docker compose down -v
	docker compose up -d

provision:  ## Configure all 3 neurons via EMQX Neuron REST API
	uv run tools/provision_neuron.py
	uv run tools/provision_security.py

place-order:  ## Place one program-a order (dev helper)
	uv run tools/place_order.py --recipe program-a --qty 1

booth-reset:  ## Recover active claims and private diagnostics; preserve history
	python3 tools/demo_admin.py reset

booth-loop:  ## Start an operator-controlled automatic walkthrough loop
	python3 tools/demo_admin.py loop

booth-up: security-init  ## Start the authenticated HTTPS profile using .env.booth
	docker compose --env-file .env --env-file .env.booth -f docker-compose.yml -f docker-compose.booth.yml up -d --build

check:  ## Check device ownership, queues, recovery, freshness and authentication
	uv run --with flask==3.1.3 --with paho-mqtt==2.1.0 --with waitress==3.0.2 --with 'PyJWT[crypto]==2.10.1' python -m unittest discover -s tests -v

check-restrictions:  ## Exercise separate visitors and live broker/product/database permissions
	uv run tools/check_restrictions.py

check-ui:  ## Check UI states and bounded-viewer handoff (Node 18+)
	node --test tests/showcase-ui.test.cjs
