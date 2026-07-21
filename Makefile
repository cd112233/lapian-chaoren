.PHONY: workbench advisor-workbench demo advisor-demo test

workbench:
	python3 src/workbench_server.py

advisor-workbench:
	mkdir -p outputs/projects
	python3 src/ai_advisor_server.py --provider mock --asset-root outputs/projects

demo:
	python3 src/build_review_page.py examples/demo/demo_shots.yaml --output examples/demo/review_demo.html

advisor-demo:
	python3 src/ai_advisor_server.py --provider mock --asset-root examples/demo

test:
	python3 -m unittest discover -s tests -p 'test_*.py' -v
