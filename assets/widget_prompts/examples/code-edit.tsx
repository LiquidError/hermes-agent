/**
 * JavaScript snippet editor with live state push to the agent.
 *
 * Pattern: card-controlled editing surface. The editor owns the current text
 * locally for responsive typing; pushes its content to the agent via
 * `widget_message` (handled below) on every change so the agent sees the
 * latest snapshot. Initial code arrives via `widget.message` so the agent
 * decides what the user starts with.
 *
 * `<CodeEditor>` lives in `canvas-primitives/heavy` (lazy-loaded chunk).
 * Currently only `language="javascript"` is supported. React requires the
 * consumer to wrap lazy components in `<Suspense>`.
 *
 * Capabilities: [] (the card receives initial code via widget.message; the
 *                  agent reads change pushes via canvasAPI.onMessage on its
 *                  side. Outbound widget_message is handled by the runtime,
 *                  not this card.)
 * Imports: React (with hooks + Suspense); Card from 'canvas-primitives';
 *          CodeEditor from 'canvas-primitives/heavy';
 *          canvasAPI.onMessage for receiving code pushes.
 */

import React, { Suspense, useEffect, useState } from 'react';
import { Card } from 'canvas-primitives';
import { CodeEditor } from 'canvas-primitives/heavy';

declare const canvasAPI: {
  onMessage(handler: (msg: unknown) => void): () => void;
};

export default function CodeSnippet() {
  const [value, setValue] = useState<string>(
    "function greet(name) {\n  return `Hello, ${name}!`;\n}\n",
  );
  const [title, setTitle] = useState<string>('Snippet');
  const [readOnly, setReadOnly] = useState<boolean>(false);

  useEffect(() => {
    return canvasAPI.onMessage((msg) => {
      const m = msg as {
        kind?: string;
        code?: string;
        title?: string;
        read_only?: boolean;
      };
      if (m.kind === 'code.refresh' && typeof m.code === 'string') {
        setValue(m.code);
        if (typeof m.title === 'string') setTitle(m.title);
        if (typeof m.read_only === 'boolean') setReadOnly(m.read_only);
      }
    });
  }, []);

  return (
    <Card title={title}>
      <Suspense fallback={<div style={{ padding: 12 }}>Loading editor…</div>}>
        <CodeEditor
          value={value}
          onChange={setValue}
          language="javascript"
          readOnly={readOnly}
        />
      </Suspense>
    </Card>
  );
}
