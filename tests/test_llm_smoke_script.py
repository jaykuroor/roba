from scripts.llm_smoke import main


def test_llm_smoke_script_offline(monkeypatch, capsys):
    for var in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    assert main() == 0
    output = capsys.readouterr().out
    assert '"gemini_model": "gemini-3.1-flash-lite"' in output
    assert '"gemini_key_present": false' in output
