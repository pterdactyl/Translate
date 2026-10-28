import pytest

from integration.pipeline import discover_apps, run_app


@pytest.mark.parametrize("app", discover_apps(), ids=lambda path: path.name)
def test_application_matches_python(app, tmp_path):
    result = run_app(app, tmp_path / app.name)
    assert result.ok, result.report()
