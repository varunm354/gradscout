from gradscout.config import load_config
from gradscout.models import AlertPriority


def test_example_config_loads_and_validates():
    cfg = load_config("config.example.yaml")
    assert cfg.candidate.graduation_year == 2027
    assert "backend" in cfg.candidate.resume_variants
    # notification defaults reflect failure-first / no-lost-alerts design
    assert cfg.notifications.send_healthy_reports is False
    assert cfg.notifications.discord_min_priority == AlertPriority.p2
    # watchlist entries carry configurable company_priority
    assert all(w.company_priority >= 1 for w in cfg.watchlist)
