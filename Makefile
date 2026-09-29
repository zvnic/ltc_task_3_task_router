SHELL := /bin/bash
.DEFAULT_GOAL := help
.NOTPARALLEL:

export SERVICE FILE DATASET_ID PLAN_ID EVENT_FILE CONFIRM

.PHONY: help doctor init bootstrap build up dev stop down restart ps logs migrate seed data-audit import import-reference plan replan benchmark lint typecheck test e2e smoke check verify-release backup restore clean

help doctor init bootstrap build up dev stop down restart ps logs migrate seed data-audit import import-reference plan replan benchmark lint typecheck test e2e smoke check verify-release backup restore clean:
	@bash scripts/project.sh "$@"
