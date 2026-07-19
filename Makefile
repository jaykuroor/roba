base:
	docker compose build base

up: base
	docker compose up --build

down:
	docker compose down

reset: base
	docker compose down -v
	docker compose up --build

seed:
	curl -s -X POST http://localhost:8000/api/seed/preset/bellas_kitchen | python3 -m json.tool

# Multi-restaurant manager (docs/fable/manager-dashboard.md).
# Local dev: run this + `cd frontend && npm run dev`, then open /admin.
manager:
	.venv/bin/uvicorn manager:app --host 0.0.0.0 --port 8100

demo-a: base
	DEMO_MODE=track_a docker compose up --build

demo-b: base
	DEMO_MODE=track_b docker compose up --build

demo: base
	DEMO_MODE=combined docker compose up --build

test:
	.venv/bin/pytest tests/ track_b/tests/ -v
	cd frontend && npm run test -- --run
