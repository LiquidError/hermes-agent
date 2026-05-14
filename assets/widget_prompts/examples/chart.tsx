/**
 * Bar/line/pie chart driven by agent-pushed data via widget.message.
 *
 * Pattern: data-driven, agent-pushed updates. The card subscribes to
 * `canvasAPI.onMessage` and re-renders whenever the agent pushes a structured
 * dataset via `widget_message`. Good for dashboards, time-series, comparison
 * snapshots — anywhere the agent has already computed the rows and just
 * needs them displayed.
 *
 * `<Chart>` lives in `canvas-primitives/heavy` (lazy-loaded chunk). React
 * requires the consumer to wrap lazy components in `<Suspense>`; the agent
 * ships a fallback that reads as "loading…" while the chunk fetches. The
 * chunk is cached for the mount, so subsequent re-renders are zero-cost.
 *
 * Capabilities: [] (the agent pushes data via widget_message; the card
 *                  doesn't need to call back)
 * Imports: React (with hooks + Suspense); Card from 'canvas-primitives';
 *          Chart from 'canvas-primitives/heavy';
 *          canvasAPI.onMessage for receiving structured pushes.
 */

import React, { Suspense, useEffect, useState } from 'react';
import { Card } from 'canvas-primitives';
import { Chart, type ChartKind } from 'canvas-primitives/heavy';

declare const canvasAPI: {
  onMessage(handler: (msg: unknown) => void): () => void;
};

type DataPoint = { name: string; value: number };

export default function DataSnapshot() {
  const [data, setData] = useState<DataPoint[]>([]);
  const [kind, setKind] = useState<ChartKind>('bar');
  const [title, setTitle] = useState<string>('Snapshot');

  useEffect(() => {
    return canvasAPI.onMessage((msg) => {
      const m = msg as {
        kind?: string;
        chart_kind?: ChartKind;
        data?: DataPoint[];
        title?: string;
      };
      if (m.kind === 'data.refresh' && Array.isArray(m.data)) {
        setData(m.data);
        if (typeof m.title === 'string') setTitle(m.title);
        if (m.chart_kind === 'bar' || m.chart_kind === 'line' || m.chart_kind === 'pie') {
          setKind(m.chart_kind);
        }
      }
    });
  }, []);

  return (
    <Card title={title}>
      <Suspense fallback={<div style={{ height: 200 }}>Loading chart…</div>}>
        <Chart data={data} kind={kind} />
      </Suspense>
    </Card>
  );
}
