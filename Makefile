up:
	docker compose up --build

down:
	docker compose down

reset:
	docker compose down -v
	docker compose up --build

seed:
	curl -s -X POST http://localhost:8000/api/seed/preset/bellas_kitchen | python3 -m json.tool

demo-a:
	DEMO_MODE=track_a docker compose up --build

demo-b:
	DEMO_MODE=track_b docker compose up --build

demo:
	DEMO_MODE=combined docker compose up --build

test:
	.venv/bin/pytest tests/ track_b/tests/ -v
	cd frontend && npm run test -- --run
