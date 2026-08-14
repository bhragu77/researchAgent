.PHONY: help eval eval-fast eval-clean serve ingest test

PYTHON ?= .venv/bin/python

help:
	@echo "make eval       - run the offline evaluation gate (30 cases, throttled + cached)"
	@echo "make eval-fast  - same gate with no throttling (paid keys / warm cache only)"
	@echo "make eval-clean - drop the cached LLM responses and re-run from scratch"
	@echo "make serve      - run the API locally"
	@echo "make ingest     - rebuild the corpus index"
	@echo "make test       - run the unit tests"

# Throttled to respect free-tier RPM, and cached so a re-run after a scoring
# change costs nothing. Exits non-zero when the gate fails, so CI can block.
eval:
	$(PYTHON) -m app.eval.run_eval \
		--golden app/eval/golden_set.jsonl \
		--min-interval 4 \
		--sleep 2 \
		--cache-dir data/eval_cache \
		--out data/eval_results.json

eval-fast:
	$(PYTHON) -m app.eval.run_eval \
		--golden app/eval/golden_set.jsonl \
		--min-interval 0 \
		--sleep 0 \
		--cache-dir data/eval_cache \
		--out data/eval_results.json

eval-clean:
	rm -rf data/eval_cache
	$(MAKE) eval

serve:
	$(PYTHON) -m uvicorn app.main:app --reload

ingest:
	$(PYTHON) -m scripts.ingest

test:
	$(PYTHON) -m pytest tests -q
