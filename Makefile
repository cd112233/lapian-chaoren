.PHONY: workbench advisor-workbench demo advisor-demo test

PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

workbench:
	$(PYTHON) src/workbench_server.py

advisor-workbench:
	$(PYTHON) src/ai_advisor_server.py --provider mock

demo:
	$(PYTHON) src/build_review_page.py examples/demo/demo_shots.yaml --output examples/demo/review_demo.html

advisor-demo:
	$(PYTHON) src/ai_advisor_server.py --provider mock --asset-root examples/demo --allow-file-origin

test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v
