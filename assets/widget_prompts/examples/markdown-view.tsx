/**
 * Render agent-pushed markdown as sanitized HTML.
 *
 * Pattern: data-driven, agent-pushed updates. The card subscribes to
 * `canvasAPI.onMessage` and re-renders whenever the agent ships a new
 * markdown string via `widget_message`. Good for prose results, summaries,
 * and any agent output that benefits from headings, lists, links, and
 * inline code without the agent shipping pre-rendered HTML.
 *
 * `<MarkdownView>` lives in `canvas-primitives/heavy` (lazy-loaded chunk).
 * Sanitization (DOMPurify) is built into the primitive — agents may emit
 * arbitrary text including hostile HTML, and the iframe sandbox is the
 * trust boundary, not the agent. React requires the consumer to wrap lazy
 * components in `<Suspense>`.
 *
 * Capabilities: [] (the agent pushes content via widget_message; the card
 *                  doesn't need to call back)
 * Imports: React (with hooks + Suspense); Card from 'canvas-primitives';
 *          MarkdownView from 'canvas-primitives/heavy';
 *          canvasAPI.onMessage for receiving structured pushes.
 */

import React, { Suspense, useEffect, useState } from 'react';
import { Card } from 'canvas-primitives';
import { MarkdownView } from 'canvas-primitives/heavy';

declare const canvasAPI: {
  onMessage(handler: (msg: unknown) => void): () => void;
};

export default function MarkdownReader() {
  const [source, setSource] = useState<string>('');
  const [title, setTitle] = useState<string>('Notes');

  useEffect(() => {
    return canvasAPI.onMessage((msg) => {
      const m = msg as { kind?: string; markdown?: string; title?: string };
      if (m.kind === 'markdown.refresh' && typeof m.markdown === 'string') {
        setSource(m.markdown);
        if (typeof m.title === 'string') setTitle(m.title);
      }
    });
  }, []);

  return (
    <Card title={title}>
      <Suspense fallback={<div style={{ padding: 12 }}>Loading…</div>}>
        <MarkdownView source={source} />
      </Suspense>
    </Card>
  );
}
