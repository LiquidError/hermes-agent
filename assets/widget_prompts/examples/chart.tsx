/**
 * Chart over agent-supplied data, refreshed via widget.message.
 *
 * Pattern: data-driven, agent-pushed updates. The card renders a chart of
 * whatever data the agent pushes via widget_message. Good for dashboards,
 * comparisons, time series, and any case where the agent has already
 * computed a structured dataset and just needs to display it.
 *
 * Capabilities: [] (the agent pushes data via widget_message; the card
 *                  doesn't need to call back)
 * Imports: React (with hooks); Card, Chart from 'canvas-primitives';
 *          canvasAPI.onMessage for receiving structured pushes.
 */

import React, { useEffect, useState } from 'react';
import { Card, Chart, Text } from 'canvas-primitives';

declare const canvasAPI: {
  onMessage(handler: (msg: unknown) => void): () => void;
};

type DataPoint = { label: string; value: number };

export default function DataChart() {
  const [data, setData] = useState<DataPoint[]>([]);
  const [title, setTitle] = useState<string>('Chart');

  useEffect(() => {
    return canvasAPI.onMessage((msg) => {
      const m = msg as { kind?: string; data?: DataPoint[]; title?: string };
      if (m.kind === 'data.refresh' && Array.isArray(m.data)) {
        setData(m.data);
        if (typeof m.title === 'string') setTitle(m.title);
      }
    });
  }, []);

  if (data.length === 0) {
    return <Card title={title}><Text muted>Awaiting data…</Text></Card>;
  }

  return (
    <Card title={title}>
      <Chart data={data} kind="bar" />
    </Card>
  );
}
