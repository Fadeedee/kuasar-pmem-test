PYTHON ?= python3
HARNESS := harness
FORMAL_SCRIPTS := \
	benchmark_metrics.py \
	benchmark_workloads.py \
	run_benchmark_worker.py \
	run_reuse_benchmark.py \
	run_three_path_evidence.py \
	analyze_three_path_evidence.py \
	run_cache_backing_evidence.py \
	analyze_cache_backing.py \
	run_full_tree_backing_evidence.py \
	analyze_full_tree_backing.py \
	run_cache_backing_pressure.py \
	render_cache_backing_svg.py \
	render_full_tree_backing_svg.py \
	render_backing_pressure_svg.py

.PHONY: check compile test audit reanalyze

check: compile test audit

compile:
	cd $(HARNESS) && $(PYTHON) -m py_compile $(FORMAL_SCRIPTS)

test:
	cd $(HARNESS) && $(PYTHON) -m unittest discover -v -p 'test_*.py'

audit:
	$(PYTHON) tools/verify_archive.py

reanalyze:
	$(PYTHON) tools/verify_archive.py --reanalyze

