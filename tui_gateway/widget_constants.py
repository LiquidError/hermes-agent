"""Wire-contract constants for the widget runtime.

Values here are SHARED with the Tauri side via the source spec
(plans/hermes-widget-render-spec.md §3.5.3, §8). Changing a value here
without changing the matching constant in the Tauri client breaks the
contract — please update both sides together.
"""

# 32 KiB cap on widget.api_response.result, enforced server-side before emit.
HERMES_ASK_RESPONSE_CAP_BYTES = 32 * 1024

# Error codes — Hermes-side (4xxx) + cross-side (5xxx). Code 4105 is reserved
# in the wire spec for a future per-call approval gate; not allocated here
# until that flow lands.
ERROR_UNKNOWN_CAPABILITY = 4101
ERROR_SOURCE_TOO_LARGE = 4102
ERROR_UNKNOWN_CARD = 4103
ERROR_CAP_NOT_DECLARED = 4104
ERROR_RESPONSE_TOO_LARGE = 4106
ERROR_MESSAGE_TOO_LARGE = 4107

ERROR_CLIENT_REFUSED_MOUNT = 5101
ERROR_RENDER_TIMED_OUT = 5102
ERROR_API_CALL_EXPIRED = 5103
