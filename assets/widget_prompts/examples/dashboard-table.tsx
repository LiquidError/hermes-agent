/**
 * Sortable, paginated table driven by agent-pushed rows via widget.message.
 *
 * Pattern: data-driven, agent-pushed updates. The card subscribes to
 * `canvasAPI.onMessage` and re-renders whenever the agent ships a new dataset
 * via `widget_message`. Good for any tabular result the agent produces — query
 * results, search hits, log dumps — that benefits from sorting and paging
 * without an extra round trip per page.
 *
 * `<Table>` lives in `canvas-primitives/heavy` (lazy-loaded chunk). React
 * requires the consumer to wrap lazy components in `<Suspense>`. The chunk
 * is cached for the mount, so re-renders triggered by data updates are
 * zero-cost.
 *
 * Capabilities: [] (the agent pushes rows via widget_message; the card
 *                  doesn't need to call back)
 * Imports: React (with hooks + Suspense); Card from 'canvas-primitives';
 *          Table from 'canvas-primitives/heavy';
 *          canvasAPI.onMessage for receiving row pushes.
 */

import React, { Suspense, useEffect, useState } from 'react';
import { Card } from 'canvas-primitives';
import { Table } from 'canvas-primitives/heavy';

declare const canvasAPI: {
  onMessage(handler: (msg: unknown) => void): () => void;
};

interface Row {
  name: string;
  value: number;
  status: string;
}

const COLUMNS = [
  { id: 'name', header: 'Name', accessor: (r: Row) => r.name },
  { id: 'value', header: 'Value', accessor: (r: Row) => r.value },
  { id: 'status', header: 'Status', accessor: (r: Row) => r.status },
];

export default function DashboardTable() {
  const [rows, setRows] = useState<Row[]>([]);
  const [title, setTitle] = useState<string>('Results');

  useEffect(() => {
    return canvasAPI.onMessage((msg) => {
      const m = msg as { kind?: string; rows?: Row[]; title?: string };
      if (m.kind === 'rows.refresh' && Array.isArray(m.rows)) {
        setRows(m.rows);
        if (typeof m.title === 'string') setTitle(m.title);
      }
    });
  }, []);

  return (
    <Card title={title}>
      <Suspense fallback={<div style={{ padding: 12 }}>Loading table…</div>}>
        <Table
          data={rows}
          columns={COLUMNS}
          pageSize={10}
        />
      </Suspense>
    </Card>
  );
}
