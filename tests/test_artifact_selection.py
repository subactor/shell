from subactor_shell.artifacts import select_relevant_text


def test_local_lexical_selection_prefers_relevant_chunk_and_respects_budget():
    content = (
        "intro " * 500
        + "\nDEPLOYMENT TLS certificate renewal connector readiness production\n"
        + "tail " * 500
    )
    selected, truncated = select_relevant_text(
        content,
        "check TLS certificate and connector readiness",
        max_chars=900,
        chunk_chars=420,
        max_chunks=2,
    )
    assert truncated is True
    assert "TLS certificate renewal" in selected
    assert len(selected) <= 900
