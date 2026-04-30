"""Card IDs from the server allocator match the validator the Tauri side uses.

Cross-machine alignment: the format /^wgt_[0-9a-f]{6}$/ is shared
verbatim. Tauri validates incoming widget.render events against this
exact regex; producing a non-matching id would crash mount.
"""

import re

from tui_gateway.widget_runtime import WidgetRegistry

CANONICAL_RE = re.compile(r"^wgt_[0-9a-f]{6}$")


def test_allocator_produces_canonical_format():
    reg = WidgetRegistry()
    for _ in range(200):
        cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)
        assert CANONICAL_RE.match(cid), f"non-canonical card_id: {cid!r}"


def test_allocator_avoids_collisions_in_one_session():
    reg = WidgetRegistry()
    seen = {reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None) for _ in range(500)}
    assert len(seen) == 500
