HERMES_SRC ?= $(HOME)/hermes-agent
PY ?= .venv/bin/python

.PHONY: test test-unit test-live smoke lint

test:
	HERMES_SRC=$(HERMES_SRC) $(PY) -m pytest tests -qu

test-unit:
	HERMES_SRC=$(HERMES_SRC) $(PY) -m pytest tests -q --ignore=tests/integration

# Needs a reachable API:
#   kubectl -n sandbox-system port-forward pod/<api-pod> 18081:3000
#   make test-live SANDBOX_API_URL=http://127.0.0.1:18081
test-live:
	HERMES_SRC=$(HERMES_SRC) $(PY) -m pytest tests/integration -v

smoke:
	SANDBOX_API_URL=$(SANDBOX_API_URL) $(PY) smoke.py

lint:
	$(PY) -m ruff check hermes_sandbox tests
