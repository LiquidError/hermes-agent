/**
 * Reorderable list with per-card persistent state.
 *
 * Pattern: stateful + persisted. The card maintains its own list of items
 * across re-mounts via canvasAPI.storage. Good for trackers, todos,
 * configurations, and anything the user expects to find unchanged when
 * they come back.
 *
 * Capabilities: ['storage.get', 'storage.set']
 * Imports: React (with hooks); Card, Stack, Button, Text, Row from 'canvas-primitives'.
 */

import React, { useEffect, useState } from 'react';
import { Card, Stack, Button, Text, Row } from 'canvas-primitives';

declare const canvasAPI: {
  storage: {
    get(key: string): Promise<unknown>;
    set(key: string, value: unknown): Promise<void>;
  };
};

const KEY = 'tracker.items';

export default function TrackerCard() {
  const [items, setItems] = useState<string[]>([]);
  const [draft, setDraft] = useState('');
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    canvasAPI.storage.get(KEY).then((value) => {
      if (Array.isArray(value)) setItems(value as string[]);
      setHydrated(true);
    });
  }, []);

  const persist = (next: string[]) => {
    setItems(next);
    canvasAPI.storage.set(KEY, next);
  };

  const add = () => {
    if (!draft.trim()) return;
    persist([...items, draft.trim()]);
    setDraft('');
  };

  const remove = (i: number) => persist(items.filter((_, j) => j !== i));

  if (!hydrated) return <Card title="Tracker"><Text variant="muted">Loading…</Text></Card>;

  return (
    <Card title="Tracker">
      <Stack gap={2}>
        {items.map((item, i) => (
          <Row key={i}>
            <Text>{item}</Text>
            <Button onClick={() => remove(i)} variant="ghost">Remove</Button>
          </Row>
        ))}
        <Row>
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="New item…"
          />
          <Button onClick={add}>Add</Button>
        </Row>
      </Stack>
    </Card>
  );
}
