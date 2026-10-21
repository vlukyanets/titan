.PHONY: all check editorconfig-check clean

all: check

# Lint, type check and tests of everything; CI runs the same target.
check: editorconfig-check
	cd backend && uv run ruff check . && uv run ruff format --check . \
		&& uv run mypy . && uv run pytest
	cd cli && test -z "$$(gofmt -l .)" && go vet ./... && go test ./...

# Every file follows .editorconfig. Go files are left to gofmt: the usage
# text lines up its columns with spaces.
editorconfig-check:
	go run github.com/editorconfig-checker/editorconfig-checker/v3/cmd/editorconfig-checker@v3.11.3 -exclude '\.go$$'

# Removes what the checks leave behind; .venv stays.
clean:
	cd backend && rm -rf .pytest_cache .mypy_cache .ruff_cache
	find backend -path backend/.venv -prune -o -name __pycache__ -type d -prune -exec rm -rf {} +
