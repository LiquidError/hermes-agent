/**
 * Form with a "fill in" button that calls hermes.ask to populate fields.
 *
 * Pattern: round-trip. The card collects user input AND can ask the agent
 * to fill known fields. Demonstrates the canvasAPI.hermes.ask round-trip
 * with the async accept/correlate/respond flow handled invisibly by the
 * Tauri broker.
 *
 * Capabilities: ['hermes.ask', 'notes.save']
 * Imports: React (with hooks); Card, Field, Button, Stack from 'canvas-primitives'.
 */

import React, { useState, useCallback } from 'react';
import { Card, Field, Button, Stack, Row, Text } from 'canvas-primitives';

declare const canvasAPI: {
  hermes: { ask(prompt: string): Promise<string> };
  notes: { save(args: { title: string; body: string; tags?: string[] }): Promise<{ note_id: string }> };
};

export default function RetroForm() {
  const [wins, setWins] = useState('');
  const [misses, setMisses] = useState('');
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);

  const fillFromAgent = useCallback(async () => {
    setBusy(true);
    try {
      const summary = await canvasAPI.hermes.ask(
        'Return two markdown bullet-list sections separated exactly by "\\n---\\n": first wins, then misses.'
      );
      const [w = '', m = ''] = summary.split('\n---\n', 2);
      if (w.trim()) setWins(w.trim());
      if (m.trim()) setMisses(m.trim());
    } finally {
      setBusy(false);
    }
  }, []);

  const save = useCallback(async () => {
    if (saving) return;
    setSaving(true);
    try {
      await canvasAPI.notes.save({
        title: 'Q3 retro',
        body: `## Wins\n${wins}\n\n## Misses\n${misses}`,
        tags: ['retro', 'quarterly'],
      });
    } finally {
      setSaving(false);
    }
  }, [wins, misses, saving]);

  return (
    <Card title="Q3 retro">
      <Stack gap={3}>
        <Field label="Wins">
          <textarea value={wins} onChange={(e) => setWins(e.target.value)} rows={6} />
        </Field>
        <Field label="Misses">
          <textarea value={misses} onChange={(e) => setMisses(e.target.value)} rows={6} />
        </Field>
        <Row gap={2}>
          <Button onClick={fillFromAgent} disabled={busy}>
            {busy ? 'Asking…' : 'Fill from agent'}
          </Button>
          <Button onClick={save} variant="primary" disabled={saving}>
            {saving ? 'Saving…' : 'Save as note'}
          </Button>
        </Row>
        {busy && <Text variant="muted">Hermes is thinking…</Text>}
      </Stack>
    </Card>
  );
}
