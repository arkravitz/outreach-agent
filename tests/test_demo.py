import runpy
from pathlib import Path
import pytest
from conftest import SKILL_IS_STUB


@pytest.mark.skipif(SKILL_IS_STUB, reason="requires the installed no-ai-slop skill")
def test_full_offline_customer_lifecycle(tmp_path):
    demo = runpy.run_path(str(Path(__file__).parents[1] / "examples/dry_run.py"))
    result = demo["run"](tmp_path)
    assert result["external_emails_sent"] == 0
    assert result["mock_emails_sent"] == 1
    assert result["pending_followup_cancelled"]
    assert result["duplicate_reply_ignored"]
    assert result["mock_calendar_events"] == 1
    assert result["project_state"] == "PROJECT_COMPLETED"
