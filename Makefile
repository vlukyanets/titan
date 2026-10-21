.PHONY: all check editorconfig-check backend-check cli-check clean

TEST_DB = docker compose -f backend/tests/compose.test.yaml

all: check

# Lint, type check and tests of everything; CI runs the same target.
check: editorconfig-check backend-check cli-check

# Every file follows .editorconfig. Go files are left to gofmt: the usage
# text lines up its columns with spaces.
editorconfig-check:
	go run github.com/editorconfig-checker/editorconfig-checker/v3/cmd/editorconfig-checker@v3.11.3 -exclude '\.go$$'

# The test database lives only while the backend checks run, also when
# they fail.
backend-check:
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy .
	$(TEST_DB) up -d --wait || { $(TEST_DB) down; exit 1; }
	(cd backend && TITAN_TEST_DATABASE_URL=postgresql+psycopg://titan:test@127.0.0.1:55432/titan \
		uv run pytest); status=$$?; $(TEST_DB) down; exit $$status

cli-check:
	cd cli && test -z "$$(gofmt -l .)" && go vet ./... && go test ./...

# Removes what the checks leave behind, the test database too when a run
# was stopped; .venv stays.
clean:
	$(TEST_DB) down
	cd backend && rm -rf .pytest_cache .mypy_cache .ruff_cache
	find backend -path backend/.venv -prune -o -name __pycache__ -type d -prune -exec rm -rf {} +
