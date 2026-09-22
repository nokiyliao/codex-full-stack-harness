PYTHON ?= python3
SOURCE_DATE_EPOCH ?= $(shell git log -1 --format=%ct 2>/dev/null)
export SOURCE_DATE_EPOCH

.PHONY: build check check-components clean-dist demo evidence-integrity install-dev installed-smoke provenance release-check review smoke test verify-dist

install-dev:
	$(PYTHON) -m pip install -e '.[dev]'

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

demo:
	PYTHONPATH=src $(PYTHON) examples/synthetic_demo.py

smoke:
	PYTHONPATH=src $(PYTHON) -c "import codex_collaboration_harness; print(codex_collaboration_harness.__name__)"

installed-smoke:
	$(PYTHON) -I -c "import importlib.metadata as metadata; import codex_collaboration_harness as harness; print(metadata.version('codex-collaboration-harness'), harness.__name__)"

provenance:
	$(PYTHON) scripts/check_provenance.py

evidence-integrity: provenance

review:
	$(PYTHON) scripts/review_readiness.py

check: review provenance test demo smoke

check-components:
	$(PYTHON) scripts/check_components.py

clean-dist:
	$(PYTHON) scripts/clean_release_state.py

build:
	$(PYTHON) -m build --sdist --wheel
	$(PYTHON) scripts/normalize_sdist.py

verify-dist:
	$(PYTHON) scripts/verify_dist.py

release-check: check
	$(MAKE) clean-dist PYTHON="$(PYTHON)"
	$(MAKE) build PYTHON="$(PYTHON)"
	$(MAKE) verify-dist PYTHON="$(PYTHON)"
	$(PYTHON) scripts/verify_reproducible_dist.py
