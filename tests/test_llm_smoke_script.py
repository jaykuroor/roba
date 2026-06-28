from scripts.llm_smoke import main


def test_llm_smoke_script_offline(monkeypatch, capsys):
    for var in ("GOOGLE_CLOUD_PROJECT",):
        monkeypatch.delenv(var, raising=False)

    assert main() == 0
    output = capsys.readouterr().out
    assert '"gemini_model": "gemini-3.1-flash-lite"' in output
    assert '"gcp_project_present": false' in output
