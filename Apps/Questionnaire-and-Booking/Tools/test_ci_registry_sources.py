"""Protect the pinned, CI-only image source override from Docker Hub regressions."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
APP = ROOT / 'Apps/Questionnaire-and-Booking'
OVERRIDE = (ROOT / '.github/ci-compose-images.yml').read_text()
WORKFLOW = (ROOT / '.github/workflows/ci.yml').read_text()
BASE_COMPOSE = (APP / 'backend/docker/docker-compose.yml').read_text()
BACKEND_DOCKERFILE = (APP / 'backend/docker/Dockerfile').read_text()
FRONTEND_DOCKERFILE = (APP / 'frontend/Dockerfile').read_text()


def test_ci_override_pins_each_official_registry_manifest_and_platform_base():
    assert 'postgres:15-alpine@sha256:f7d23353e1b15400d22ebe31189f4d314b87a4c129cc400c8c2d8d4ca127bf81' in OVERRIDE
    assert 'python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1' in OVERRIDE
    assert 'node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402' in OVERRIDE
    assert 'mailpit:v1.27.0@sha256:fb84cfb1c3a33007cfdee8ba5a77b8c6c894de5b1a368372ddc5c965cf5c0b6d' in OVERRIDE
    assert OVERRIDE.count('pull_policy: never') == 2
    assert all(f'  {service}:' in OVERRIDE for service in (
        'pg-contacts', 'pg-enquiries', 'pg-responses', 'pg-tokens', 'pg-settings', 'pg-sandbox'))
    assert 'ARG PYTHON_BASE_IMAGE=python:3.12-slim' in BACKEND_DOCKERFILE
    assert 'FROM ${PYTHON_BASE_IMAGE}' in BACKEND_DOCKERFILE
    assert 'ARG NODE_BASE_IMAGE=node:22-alpine' in FRONTEND_DOCKERFILE
    assert 'FROM ${NODE_BASE_IMAGE}' in FRONTEND_DOCKERFILE


def test_ci_preloads_pinned_mirrors_sequentially_and_uses_override_for_every_compose_action():
    assert WORKFLOW.count('docker pull --platform linux/amd64') == 4
    assert WORKFLOW.count('sleep 2') >= 2
    assert 'registry_override=../../.github/ci-compose-images.yml' in WORKFLOW
    compose_lines=[line.strip() for line in WORKFLOW.splitlines() if 'docker compose ' in line]
    assert len(compose_lines)==4
    assert all('registry_override' in line or 'ci-compose-images.yml' in line for line in compose_lines)
    assert 'up --build --pull never -d' in WORKFLOW
    assert 'docker.io/' not in WORKFLOW
    assert 'ghcr.io/axllent/mailpit:' in WORKFLOW
    assert 'public.ecr.aws/docker/library/postgres:' in WORKFLOW


def test_production_compose_defaults_remain_unchanged_and_ci_override_is_outside_app_tree():
    assert 'image: postgres:15-alpine' in BASE_COMPOSE
    assert 'image: axllent/mailpit:v1.27.0' in BASE_COMPOSE
    assert OVERRIDE
    assert (ROOT / '.github/ci-compose-images.yml').is_relative_to(ROOT / '.github')
