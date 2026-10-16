PYTHON ?= python
.PHONY: install test lint up down benchmark-mock load-test-mock
install:
	$(PYTHON) scripts/manage.py install
test:
	$(PYTHON) scripts/manage.py test
lint:
	$(PYTHON) scripts/manage.py lint
up:
	$(PYTHON) scripts/manage.py up
down:
	$(PYTHON) scripts/manage.py down
benchmark-mock:
	$(PYTHON) scripts/manage.py benchmark-mock
load-test-mock:
	$(PYTHON) scripts/manage.py load-test-mock
