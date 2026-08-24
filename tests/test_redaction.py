from subactor_shell.redaction import ExactRedactor, StreamingRedactor


def test_exact_redactor_prefers_long_values():
    redactor = ExactRedactor(["abc", "abcdef"])
    assert redactor.redact("xabcdefyabc") == "x[REDACTED]y[REDACTED]"


def test_streaming_redactor_never_leaks_split_secret():
    redactor = StreamingRedactor(["token-super-secret"])
    output = "".join(
        [
            redactor.feed("prefix token-"),
            redactor.feed("super-"),
            redactor.feed("secret suffix"),
            redactor.finish(),
        ]
    )
    assert output == "prefix [REDACTED] suffix"
    assert "token-" not in output
    assert "super-secret" not in output


def test_streaming_redactor_without_values_streams_normally():
    redactor = StreamingRedactor([])
    assert redactor.feed("abc") + redactor.finish() == "abc"
