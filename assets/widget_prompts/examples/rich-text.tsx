/**
 * Rich-text editor with live state push to the agent.
 *
 * Pattern: card-controlled editing surface. The editor owns the current HTML
 * locally for responsive typing; the agent pushes initial content via
 * `widget_message`. Edits surface as new HTML on every change so an outer
 * caller (host or, eventually, the agent via a future capability) can
 * persist the document.
 *
 * `<RichTextEditor>` lives in `canvas-primitives/heavy` (lazy-loaded chunk).
 * Backed by Tiptap + StarterKit (bold/italic/strike/headings/blockquote/
 * lists/code/undo). React requires the consumer to wrap lazy components in
 * `<Suspense>`.
 *
 * Capabilities: [] (the card receives initial HTML via widget.message;
 *                  changes are kept locally — adding a server round-trip
 *                  on every keystroke is anti-pattern. The agent reads
 *                  state from a future `notes.save` or similar capability
 *                  when the user explicitly commits.)
 * Imports: React (with hooks + Suspense); Card from 'canvas-primitives';
 *          RichTextEditor from 'canvas-primitives/heavy';
 *          canvasAPI.onMessage for receiving HTML pushes.
 */

import React, { Suspense, useEffect, useState } from 'react';
import { Card } from 'canvas-primitives';
import { RichTextEditor } from 'canvas-primitives/heavy';

declare const canvasAPI: {
  onMessage(handler: (msg: unknown) => void): () => void;
};

export default function RichTextDoc() {
  const [value, setValue] = useState<string>(
    '<h2>Untitled</h2><p>Start writing…</p>',
  );
  const [title, setTitle] = useState<string>('Document');
  const [editable, setEditable] = useState<boolean>(true);

  useEffect(() => {
    return canvasAPI.onMessage((msg) => {
      const m = msg as {
        kind?: string;
        html?: string;
        title?: string;
        editable?: boolean;
      };
      if (m.kind === 'doc.refresh' && typeof m.html === 'string') {
        setValue(m.html);
        if (typeof m.title === 'string') setTitle(m.title);
        if (typeof m.editable === 'boolean') setEditable(m.editable);
      }
    });
  }, []);

  return (
    <Card title={title}>
      <Suspense fallback={<div style={{ padding: 12 }}>Loading editor…</div>}>
        <RichTextEditor value={value} onChange={setValue} editable={editable} />
      </Suspense>
    </Card>
  );
}
