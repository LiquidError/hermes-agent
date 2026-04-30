"""Cross-machine alignment: cap and error code values must match the Tauri side.

If you change either, you must change the matching value on the Tauri
side too — they form the wire contract.
"""

from tui_gateway import widget_constants as wc


def test_response_cap_is_32_kib():
    assert wc.HERMES_ASK_RESPONSE_CAP_BYTES == 32 * 1024


def test_error_codes_match_spec_table():
    # Spec §8 — Hermes-side error codes.
    assert wc.ERROR_UNKNOWN_CAPABILITY == 4101
    assert wc.ERROR_SOURCE_TOO_LARGE == 4102
    assert wc.ERROR_UNKNOWN_CARD == 4103
    assert wc.ERROR_CAP_NOT_DECLARED == 4104
    assert wc.ERROR_RESPONSE_TOO_LARGE == 4106
    assert wc.ERROR_MESSAGE_TOO_LARGE == 4107
    assert wc.ERROR_CLIENT_REFUSED_MOUNT == 5101
    assert wc.ERROR_RENDER_TIMED_OUT == 5102
    assert wc.ERROR_API_CALL_EXPIRED == 5103
