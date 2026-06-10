from pipeline.cli import main


def test_cli_init_status_pause_and_digest_without_credentials(tmp_path, capsys):
    state = str(tmp_path / "state")
    assert main(["--state", state, "init", "--config-dir", "config"]) == 0
    assert main(["--state", state, "status"]) == 0
    data = capsys.readouterr().out
    assert "ai_automation" in data and "ai_engineering" in data
    assert main(["--state", state, "pause"]) == 0
    assert main(["--state", state, "digest"]) == 0
    assert "highest_leverage_action" in capsys.readouterr().out
    assert main(["--state", state, "resume"]) == 0


def test_cli_autonomous_authorization_needs_exact_hash(tmp_path, capsys):
    state = str(tmp_path / "state")
    main(["--state", state, "init", "--config-dir", "config"])
    assert main(["--state", state, "authorize", "ai_automation", "--hash", "wrong"]) == 2
    assert "hash" in capsys.readouterr().err.lower()


def test_cli_errors_are_readable(tmp_path, capsys):
    assert main(["--state", str(tmp_path), "inspect", "999"]) == 2
    assert "Unknown contact" in capsys.readouterr().err
