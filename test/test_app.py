import app


def test_entrypoint_exposes_only_service_health_routes():
    rules = {rule.rule for rule in app.web_app.url_map.iter_rules()}

    assert {"/healthz", "/readyz", "/api/service-health"} <= rules
    assert "/slack/events" not in rules


def test_healthz_does_not_require_runtime():
    response = app.web_app.test_client().get("/healthz")

    assert response.status_code == 200
